# coding=utf-8
import os
import time
import requests
import yaml
import logging
import json
from datetime import datetime, timedelta
from pathlib import Path
import re
import pytz
from dotenv import load_dotenv
from typing import Dict, List, Tuple, Optional, Union

# 加载 .env 文件
load_dotenv()

# 新增 Selenium 相关库
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver import ActionChains
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    WebDriverException,
    NoSuchElementException,
    TimeoutException,
)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# === 配置日志 ===
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# === 配置部分 (请在此处修改目标URL和判断逻辑) ===
TARGET_URL = os.environ.get("TARGET_URL")  # 目标页面URL
CHECK_INTERVAL = 60  # 检查间隔(秒)
LEAST_TIME_LENGTH = int(
    os.environ.get("LEAST_TIME_LENGTH", "1")
)  # 最短时间长度(小时)，字符串形式以便后续处理

# cookies 保存路径（与 docker-compose 的 ./data 挂载一致）
COOKIES_PATH = Path("./data/browser_cookies.json")

# 已提醒记忆文件（用于抑制重复提醒），建议在 compose 中挂载 `./data:/data`
MEMORY_PATH = Path("./data/seen_slots.json")
# 在同一天内允许的提醒次数阈值（超过该值后将抑制后续提醒），默认允许2次（即第1、2次会提醒，第3次开始抑制）
try:
    MEMORY_THRESHOLD = int(os.environ.get("MEMORY_THRESHOLD", "2"))
except Exception:
    MEMORY_THRESHOLD = 2

# === 核心功能 ===


def get_check_days_count() -> int:
    """
    根据当前时间确定需要监控的天数
    规则: 18:00之前只能预定今天及之后两天(共3天)，18:00之后可以预定今天及之后三天(共4天)
    """
    now = datetime.now(pytz.timezone("Asia/Shanghai"))
    if now.hour < 18:
        return 3
    else:
        return 4


def get_beijing_time():
    """获取北京时间"""
    return datetime.now(pytz.timezone("Asia/Shanghai"))


def process_report_data(report_data: Dict, report_type: str = "text"):
    """处理报告数据，生成文本内容"""
    if report_type == "text":
        content = report_data.get("message", "")
        include_morning = os.environ.get("INCLUDE_MORNING", "false").lower() == "true"
        if not include_morning:
            # 过滤掉包含“上午”的行
            filtered_lines = [
                line for line in content.split("\n") if "上午" not in line
            ]
            content = "\n".join(filtered_lines)

        return content
    return report_data


def save_cookies(driver, path: Path = COOKIES_PATH) -> None:
    """将当前浏览器 cookies 保存到文件，以便下次恢复登录状态"""
    try:
        cookies = driver.get_cookies()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False)
        logger.info(f"已保存 cookies 到 {path}")
    except Exception as e:
        logger.warning(f"保存 cookies 失败: {e}")


def load_cookies(driver, url: str, path: Path = COOKIES_PATH) -> bool:
    """
    尝试从文件加载 cookies 并注入到浏览器。
    按 cookie 的 domain 分组，逐域打开页面注入以满足同源限制。
    返回 True 表示至少注入了一个 cookie。
    """
    if not path.exists():
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    except Exception as e:
        logger.warning(f"读取 cookies 失败: {e}")
        return False

    try:
        # 按域分组 cookie
        domains = {}
        for c in cookies:
            dom = c.get("domain") or ""
            dom = dom.lstrip(".")
            if not dom:
                try:
                    dom = url.split("//", 1)[1].split("/", 1)[0]
                except Exception:
                    dom = ""
            domains.setdefault(dom, []).append(c)

        total = len(cookies)
        injected = 0

        for dom, ck_list in domains.items():
            if not dom:
                continue
            tried = False
            for scheme in ("https://", "http://"):
                target = f"{scheme}{dom}"
                try:
                    driver.get(target)
                    time.sleep(1)
                    tried = True
                    break
                except Exception:
                    continue
            if not tried:
                logger.debug(f"无法打开域以注入 cookie: {dom}")
                continue

            for c in ck_list:
                cookie = {k: v for k, v in c.items() if k not in ("sameSite",)}
                if "expiry" in cookie:
                    try:
                        cookie["expiry"] = int(cookie["expiry"])
                    except Exception:
                        cookie.pop("expiry", None)
                try:
                    driver.add_cookie(cookie)
                    injected += 1
                except Exception as e:
                    logger.debug(f"注入 cookie 到域 {dom} 失败: {e}")

        try:
            driver.refresh()
        except Exception:
            pass

        logger.info(f"尝试注入 cookies: 总共 {total} 个，成功注入 {injected} 个")
        return injected > 0
    except Exception as e:
        logger.warning(f"注入 cookies 时出错: {e}")
        return False


def load_memory(path: Path = None) -> dict:
    """加载已提醒记忆文件，返回 dict: {slot_id: {count:int, last_seen: 'YYYY-MM-DD'}}"""
    if path is None:
        path = MEMORY_PATH
    try:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
    except Exception as e:
        logger.warning(f"读取提醒记忆失败: {e}")
        return {}


def save_memory(mem: dict, path: Path = None) -> None:
    if path is None:
        path = MEMORY_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"保存提醒记忆失败: {e}")


def filter_messages_by_memory(
    messages: List[str], threshold: int = None
) -> Tuple[List[str], dict]:
    """根据记忆过滤 messages。
    messages: 原始每行消息列表（每行代表一个场地/时间的字符串），
    返回 (to_notify_list, updated_memory_dict)。
    逻辑：同一天内每个 slot_id 记录 count；当 count > threshold 时抑制该条消息。
    """
    if threshold is None:
        threshold = MEMORY_THRESHOLD
    today = get_beijing_time().strftime("%Y-%m-%d")
    mem = load_memory()
    updated = dict(mem)  # shallow copy
    to_notify = []

    for m in messages:
        slot_id = m.strip()
        if not slot_id:
            continue

        entry = updated.get(slot_id)
        if entry and entry.get("last_seen") == today:
            count = int(entry.get("count", 0)) + 1
        else:
            count = 1

        # 保存/更新内存
        updated[slot_id] = {"count": count, "last_seen": today}

        # 当 count <= threshold 时发送提醒，否则抑制
        if count <= threshold:
            to_notify.append(slot_id)

    # 清理：移除非今天的条目，避免文件膨胀
    keys_to_delete = []
    for k, v in list(updated.items()):
        if v.get("last_seen") != today:
            keys_to_delete.append(k)
    for k in keys_to_delete:
        updated.pop(k, None)

    # 持久化
    try:
        save_memory(updated)
    except Exception:
        logger.debug("保存提醒记忆时发生异常")

    return to_notify, updated


def build_continuous_periods(
    lines: List[str], include_morning: bool = True
) -> List[str]:
    """从单小时条目中构建同一天的连续时段（只按时间连续，不要求同一场地）。
    返回格式化的通知行列表，每行为: "{date_key} | 连续空余 {HH:MM}-{HH:MM} ({N}小时)"。
    期望输入行包含时间段，如 "12-02 周二 | 场地A (08:00-09:00)" 或类似格式。
    """
    pools = {}
    # 匹配左侧日期部分与时间段
    # 捕获左边任意文本（用于提取日期键）与括号内时间
    time_re = re.compile(
        r"^(?P<left>.*?)\|.*\((?P<start>\d{1,2}[:：]\d{2})\s*-\s*(?P<end>\d{1,2}[:：]\d{2})\)"
    )

    # 准备一个函数来规范化日期键（尽量提取 YYYY-MM-DD 或 MM-DD 或 中文月日）
    def normalize_date_key(left: str) -> str:
        left = left.strip()
        if not left:
            return left
        # 常见日期格式优先匹配
        dm = re.search(
            r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})|(\d{1,2}-\d{1,2})|(\d{1,2}月\d{1,2}日)",
            left,
        )
        if dm:
            return dm.group(0)
        # 否则尽量取第一个 token（例如 "12-02 周二" -> "12-02" 或 "12-02 周二 上午" -> "12-02"）
        token = left.split()[0]
        return token

    for line in lines:
        if not line:
            continue
        if not include_morning and "上午" in line:
            continue
        m = time_re.search(line)
        if not m:
            continue
        left = m.group("left")
        date_key = normalize_date_key(left)
        start = m.group("start").replace("：", ":")
        try:
            sh, sm = [int(x) for x in start.split(":")]
        except Exception:
            continue
        start_min = sh * 60 + sm
        pools.setdefault(date_key, set()).add(start_min)

    result = []
    for date_key, starts in pools.items():
        seq = sorted(starts)
        if not seq:
            continue
        cur_start = seq[0]
        cur_prev = seq[0]
        cur_len = 1
        for s in seq[1:]:
            if s == cur_prev + 60:
                cur_prev = s
                cur_len += 1
            else:
                if cur_len >= LEAST_TIME_LENGTH:
                    st_h, st_m = divmod(cur_start, 60)
                    end_min = cur_prev + 60
                    en_h, en_m = divmod(end_min, 60)
                    hours = cur_len
                    result.append(
                        f"{date_key} | 连续空余 {st_h:02d}:{st_m:02d}-{en_h:02d}:{en_m:02d} ({hours}小时)"
                    )
                cur_start = s
                cur_prev = s
                cur_len = 1

        if cur_len >= LEAST_TIME_LENGTH:
            st_h, st_m = divmod(cur_start, 60)
            end_min = cur_prev + 60
            en_h, en_m = divmod(end_min, 60)
            hours = cur_len
            result.append(
                f"{date_key} | 连续空余 {st_h:02d}:{st_m:02d}-{en_h:02d}:{en_m:02d} ({hours}小时)"
            )

    return result


def check_dates_availability(driver):
    """轮询检查每一天的场地情况"""
    days_count = get_check_days_count()
    logger.info(f"根据当前时间，将检查未来 {days_count} 天的场地情况")

    found_any = False
    messages = []

    for i in range(days_count):
        day_id = f"dayli{i}"
        try:
            logger.info(f"--- 正在检查第 {i+1} 天 (ID: {day_id}) ---")

            # 1. 找到日期标签
            # 使用 WebDriverWait 确保元素存在
            wait = WebDriverWait(driver, 10)
            day_tab = wait.until(EC.presence_of_element_located((By.ID, day_id)))

            # 获取日期文本，如 "12-02 周二"
            day_info = day_tab.text.replace("\n", " ")

            # 2. 点击切换日期
            # 优先使用 JS 调用，因为这是 onclick 定义的行为，更稳定
            # 也可以用 day_tab.click()
            driver.execute_script(f"getDateData('{i}')")
            time.sleep(2)  # 等待数据加载

            # 3. 遍历上午、下午、晚上
            # 上午: getDataTime('0'), 下午: getDataTime('1'), 晚上: getDataTime('2')
            time_periods = [
                {"code": "0", "name": "上午"},
                {"code": "1", "name": "下午"},
                {"code": "2", "name": "晚上"},
            ]

            for period in time_periods:
                logger.info(f"  检查 {period['name']}...")
                try:
                    # 切换时间段
                    # 使用 JS 直接调用页面函数，这是最直接的方式
                    driver.execute_script(f"getDataTime('{period['code']}')")
                    time.sleep(1)  # 稍作等待，确保页面UI切换完成

                    # 4. 检查当前时间段是否有空余
                    # 查找所有 class 包含 "kyd" 的 div 元素
                    # 使用 XPath 精确匹配 class='kyd'，排除 class='graphic-panel kyd' (图例)
                    available_slots = driver.find_elements(
                        By.XPATH, "//div[@class='kyd']"
                    )

                    if available_slots:
                        count = len(available_slots)
                        # logger.info(f"  -> 发现 {count} 个潜在空余元素 (含隐藏)")

                        visible_count = 0
                        found_in_period = False

                        # 提取详细信息
                        for slot in available_slots:
                            try:
                                # 关键修改：检查元素是否可见
                                # 因为页面加载了全天数据，但非当前时段的 div 是隐藏的 (display: none)
                                # 我们只处理当前可见的时段数据
                                if not slot.is_displayed():
                                    continue

                                # 获取父级 li 元素
                                parent_li = slot.find_element(By.XPATH, "./..")

                                # 尝试多种方式获取属性 (处理大小写和自定义属性问题)
                                field_name = parent_li.get_attribute("fieldname")

                                # 如果 Selenium get_attribute 仍然失败，尝试使用 JavaScript
                                if not field_name:
                                    field_name = driver.execute_script(
                                        "return arguments[0].getAttribute('fieldname')",
                                        parent_li,
                                    )

                                # 再次检查：如果没有 field_name，说明是图例元素，直接跳过
                                if not field_name:
                                    continue

                                begin_time = parent_li.get_attribute("begintime")
                                end_time = parent_li.get_attribute("endtime")

                                # 如果仍然获取不到时间，打印 HTML 以便调试
                                if not begin_time:
                                    logger.warning(
                                        f"    无法获取时间信息，元素HTML: {parent_li.get_attribute('outerHTML')[:200]}..."
                                    )

                                slot_info = f"{day_info} {period['name']} | {field_name} ({begin_time}-{end_time})"
                                messages.append(slot_info)
                                logger.info(
                                    f"    + {field_name} ({begin_time}-{end_time})"
                                )
                                visible_count += 1
                                found_in_period = True
                                found_any = True
                            except Exception as e:
                                logger.warning(f"    解析场地信息失败: {e}")

                        if visible_count > 0:
                            logger.info(f"  -> 实际可用场地: {visible_count} 个")
                        else:
                            logger.info(f"  {period['name']} 无可用名额 (可见元素为0)")

                    else:
                        logger.info(f"  {period['name']} 无名额 (未发现 class='kyd')")

                except Exception as e:
                    logger.warning(f"  检查 {period['name']} 时出错: {e}")

        except Exception as e:
            logger.error(f"检查第 {i+1} 天时出错: {e}")

    if found_any:
        return True, "\n".join(messages)
    else:
        return False, "所检查的日期内暂无名额"


def load_config():
    """加载配置文件，获取通知渠道配置"""
    config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")

    if not Path(config_path).exists():
        logger.warning(f"配置文件 {config_path} 不存在，将仅使用环境变量")
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)
            return config_data.get("notification", {}).get("webhooks", {})
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}")
        return {}


def get_webhooks():
    """获取Webhook URL"""
    webhooks = load_config()

    feishu_url_group = os.environ.get("FEISHU_WEBHOOK_URL_GROUP") or webhooks.get(
        "feishu_url_group"
    )
    feishu_url_person = os.environ.get("FEISHU_WEBHOOK_URL_PERSON") or webhooks.get(
        "feishu_url_person"
    )
    wework_url = os.environ.get("WEWORK_WEBHOOK_URL") or webhooks.get("wework_url")

    return {
        "feishu_group": feishu_url_group,
        "feishu_person": feishu_url_person,
        "wework": wework_url,
    }


def send_to_feishu(
    webhook_url: str,
    report_data: Dict,
    report_type: str,
    proxy_url: Optional[str] = None,
    rich_text: bool = True,
) -> bool:
    """发送到飞书（支持分批发送）"""
    headers = {"Content-Type": "application/json"}
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}

    content = report_data.get("message", "")
    if rich_text:
        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": "羽毛球场地剩余",
                        "content": [
                            [
                                {
                                    "tag": "text",
                                    "text": content,
                                }
                            ]
                        ],
                    }
                },
            },
        }
    else:
        payload = {
            "msg_type": "text",
            "content": {
                "text": content,
            },
        }

    try:
        response = requests.post(
            webhook_url, headers=headers, json=payload, proxies=proxies, timeout=30
        )
        if response.status_code == 200:
            result = response.json()
            # 检查飞书的响应状态
            if result.get("StatusCode") == 0 or result.get("code") == 0:
                print(f"飞书发送成功 [{report_type}]")
            else:
                error_msg = result.get("msg") or result.get("StatusMessage", "未知错误")
                print(f"飞书发送失败 [{report_type}]，错误：{error_msg}")
                return False
        else:
            print(f"飞书发送失败 [{report_type}]，状态码：{response.status_code}")
            return False
    except Exception as e:
        print(f"飞书发送出错 [{report_type}]：{e}")
        return False

    return True


def send_wework(webhook_url, title, content, url=None):
    """发送企业微信通知"""
    if not webhook_url:
        return

    headers = {"Content-Type": "application/json"}

    markdown_content = f"## {title}\n\n{content}"
    if url:
        markdown_content += f"\n\n[点击访问页面]({url})"

    payload = {"msgtype": "markdown", "markdown": {"content": markdown_content}}

    try:
        response = requests.post(webhook_url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200 and response.json().get("errcode") == 0:
            logger.info("企业微信通知发送成功")
        else:
            logger.error(f"企业微信通知发送失败: {response.text}")
    except Exception as e:
        logger.error(f"发送企业微信通知出错: {e}")


def handle_login_process(driver):
    """处理登录和协议流程"""
    try:
        # 1. 检查并点击登录
        login_buttons = driver.find_elements(
            By.XPATH,
            "//a[contains(text(), '校内统一身份认证')] | //button[contains(text(), '校内统一身份认证')] | //span[contains(text(), '校内统一身份认证')]",
        )

        if not login_buttons:
            logger.info("未检测到登录按钮，可能已登录或页面结构不同")
            # 如果没检测到登录按钮，可能是已经直接在登录页了，或者已经登录了
            # 这里可以尝试直接检测登录框是否存在，如果存在也执行登录逻辑
            if not driver.find_elements(By.ID, "password_account_input"):
                return
        else:
            logger.info("检测到未登录，点击'校内统一身份认证'按钮...")
            login_buttons[0].click()
            time.sleep(5)

        # 2. 处理用户须知界面
        logger.info("正在查找'同意协议'按钮...")

        # 通用安全点击方法：等待可见 -> 滚动 -> 常规点击 -> JS 点击 -> ActionChains 点击（重试）
        def safe_click(el, retries: int = 3):
            last_exc = None
            for attempt in range(retries):
                try:
                    # 确保元素在视窗内
                    try:
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});", el
                        )
                    except Exception:
                        pass
                    time.sleep(0.2)
                    el.click()
                    return True
                except ElementClickInterceptedException as e:
                    last_exc = e
                    try:
                        # 尝试 JS 点击（绕过遮挡）
                        driver.execute_script("arguments[0].click();", el)
                        return True
                    except Exception as e2:
                        last_exc = e2
                        try:
                            # 尝试使用 ActionChains
                            ActionChains(driver).move_to_element(el).click().perform()
                            return True
                        except Exception as e3:
                            last_exc = e3
                            time.sleep(0.5)
                            continue
                except WebDriverException as e:
                    last_exc = e
                    try:
                        driver.execute_script("arguments[0].click();", el)
                        return True
                    except Exception as e2:
                        last_exc = e2
                        time.sleep(0.5)
                        continue
            logger.debug(f"safe_click 最终失败: {last_exc}")
            return False

        # 尝试点击协议勾选框 (根据用户提供的元素特征)
        try:
            # 查找 id="iconxy" 的 i 标签
            agreement_checkbox = driver.find_elements(By.ID, "iconxy")
            if agreement_checkbox:
                logger.info("找到协议勾选框(id='iconxy')，正在点击...")
                if not safe_click(agreement_checkbox[0]):
                    logger.warning(
                        "尝试通过多种方式点击协议勾选框失败，后续将尝试通过 label 或 JS 变更属性"
                    )
                    # 备用：尝试通过 JS 设置选中状态（如果是伪复选框）
                    try:
                        driver.execute_script(
                            "arguments[0].classList.add('checked');",
                            agreement_checkbox[0],
                        )
                    except Exception:
                        pass
                time.sleep(1)
            else:
                # 备用：通过 label 文本查找前一个 i 标签
                logger.info("未通过ID找到勾选框，尝试通过文本定位...")
                agreement_checkbox = driver.find_elements(
                    By.XPATH,
                    "//label[contains(text(), '我已阅读并同意')]/preceding-sibling::i",
                )
                if agreement_checkbox:
                    if not safe_click(agreement_checkbox[0]):
                        logger.warning("通过文本定位到的勾选框点击失败，尝试 JS 点击")
                        try:
                            driver.execute_script(
                                "arguments[0].click();", agreement_checkbox[0]
                            )
                        except Exception:
                            logger.debug("JS 点击也失败，继续")
                    time.sleep(1)
        except Exception as e:
            logger.warning(f"点击协议勾选框时出错: {e}")

        # 点击'下一步'按钮
        try:
            # 优先尝试通过 ID 查找 (根据用户提供的元素特征 id="apay")
            next_button = driver.find_elements(By.ID, "apay")
            if next_button:
                logger.info("找到'下一步'按钮(id='apay')，正在点击...")
                if not safe_click(next_button[0]):
                    logger.warning("点击 '下一步' 按钮失败，尝试 JS 点击")
                    try:
                        driver.execute_script("arguments[0].click();", next_button[0])
                    except Exception as e:
                        logger.warning(f"使用 JS 点击 '下一步' 失败: {e}")
                time.sleep(3)
            else:
                # 备用：通过文本查找
                logger.info("未通过ID找到'下一步'，尝试通过文本定位...")
                next_buttons = driver.find_elements(
                    By.XPATH,
                    "//a[contains(text(), '下一步')] | //button[contains(text(), '下一步')] | //span[contains(text(), '下一步')]",
                )
                if next_buttons:
                    next_buttons[0].click()
                    time.sleep(3)
                else:
                    # 再次备用：查找同意/确定按钮
                    other_buttons = driver.find_elements(
                        By.XPATH,
                        "//button[contains(text(), '同意')] | //button[contains(text(), '确定')]",
                    )
                    if other_buttons:
                        logger.info("未找到'下一步'，尝试点击'同意/确定'...")
                        other_buttons[0].click()
                        time.sleep(3)
        except Exception as e:
            logger.warning(f"点击'下一步'按钮时出错: {e}")

        # 4. 处理登录界面
        logger.info("检查是否需要输入账号密码...")
        time.sleep(2)

        if driver.find_elements(By.ID, "password_account_input"):
            username = os.environ.get("NKU_USERNAME")
            password = os.environ.get("NKU_PASSWORD")

            if not username or not password:
                logger.error(
                    "未设置环境变量 NKU_USERNAME 或 NKU_PASSWORD，无法自动登录"
                )
                return

            logger.info("正在输入账号密码...")
            driver.find_element(By.ID, "password_account_input").clear()
            driver.find_element(By.ID, "password_account_input").send_keys(username)

            driver.find_element(By.ID, "password_password_input").clear()
            driver.find_element(By.ID, "password_password_input").send_keys(password)

            # 勾选协议 (查找 class="arco-checkbox" 且未选中的)
            logger.info("勾选登录协议...")
            try:
                # 查找所有 arco-checkbox
                checkboxes = driver.find_elements(By.CLASS_NAME, "arco-checkbox")
                for box in checkboxes:
                    # 如果没有 checked class，说明未选中，可能是协议
                    if "arco-checkbox-checked" not in box.get_attribute("class"):
                        box.click()
                        time.sleep(0.5)
            except Exception as e:
                logger.warning(f"勾选登录协议时出错: {e}")

            # 勾选记住账号 (如果需要)
            # 用户提供的 HTML 显示记住账号默认是 checked 的，所以这里不需要额外操作
            # 如果需要确保选中，可以检查 class 是否包含 arco-checkbox-checked

            # 点击登录
            logger.info("点击登录按钮...")
            login_btn = driver.find_element(By.XPATH, "//button[@type='submit']")
            login_btn.click()
            time.sleep(5)

    except Exception as e:
        logger.warning(f"登录流程出现异常 (非致命): {e}")


def navigate_to_venue(driver):
    """导航到津南羽毛球馆预约页面"""
    # 1. 点击场地预订
    logger.info("正在查找'场地预订'按钮...")
    try:
        venue_booking_btn = driver.find_elements(
            By.XPATH,
            "//div[@class='option-item' and .//div[contains(text(), '场地预订')]]",
        )
        if venue_booking_btn:
            logger.info("找到'场地预订'按钮，正在点击...")
            venue_booking_btn[0].click()
            time.sleep(5)
        else:
            logger.info("未找到按钮，尝试直接跳转到场地预订页面...")
            driver.get(
                "https://tyggl.nankai.edu.cn/Views/Venue/VenueList.html?Type=Field"
            )
            time.sleep(5)
    except Exception as e:
        raise Exception(f"进入场地预订页面失败: {e}")

    if "VenueList.html" not in driver.current_url:
        logger.warning("警告: 可能未成功进入场地预订页面")

    # 2. 点击津南校区体育馆
    logger.info("正在查找'津南校区体育馆'按钮...")
    try:
        # 使用 WebDriverWait 等待元素出现
        wait = WebDriverWait(driver, 10)
        found_gym_btn = None

        # 尝试1: 通过 onclick 精确匹配 (根据用户提供的元素特征)
        try:
            found_gym_btn = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//div[@onclick=\"gotodetail('003')\"]")
                )
            )
            logger.info("通过 onclick 找到'津南校区体育馆'按钮，点击...")
        except:
            logger.info("通过 onclick 未找到，尝试其他方式...")

        # 尝试2: 通过文本包含
        if not found_gym_btn:
            try:
                found_gym_btn = wait.until(
                    EC.element_to_be_clickable(
                        (
                            By.XPATH,
                            "//div[contains(@class, 'wrap') and .//div[contains(text(), '津南校区体育馆')]]",
                        )
                    )
                )
                logger.info("通过文本找到'津南校区体育馆'按钮，点击...")
            except:
                logger.info("通过文本未找到...")

        if found_gym_btn:
            found_gym_btn.click()
            time.sleep(5)
        else:
            # 尝试3: 直接执行 JS
            logger.info("未找到元素，尝试直接执行 JS: gotodetail('003')...")
            driver.execute_script("gotodetail('003')")
            time.sleep(5)

    except Exception as e:
        raise Exception(f"点击'津南校区体育馆'失败: {e}")

    # 3. 点击津南羽毛球馆
    logger.info("正在查找'津南羽毛球馆'按钮...")
    try:
        badminton_btn = driver.find_elements(
            By.XPATH,
            "//li[contains(@class, 'nav_typeli') and contains(text(), '津南羽毛球馆')]",
        )
        if badminton_btn:
            logger.info("找到'津南羽毛球馆'按钮，正在点击...")
            badminton_btn[0].click()
            time.sleep(5)
        else:
            logger.warning("未找到'津南羽毛球馆'按钮，尝试通过文本模糊匹配...")
            text_element = driver.find_elements(
                By.XPATH, "//li[contains(text(), '津南羽毛球馆')]"
            )
            if text_element:
                text_element[0].click()
                time.sleep(5)
            else:
                raise Exception("无法定位到'津南羽毛球馆'按钮")
    except Exception as e:
        raise Exception(f"点击'津南羽毛球馆'失败: {e}")


def check_availability():
    """检查页面是否有空余 (使用 Selenium 模拟浏览器)"""
    driver = None
    try:
        # 配置 Chrome 选项
        chrome_options = Options()
        # ==============================

        # chrome_options.add_argument("--headless")  # 调试时注释掉，运行时开启可后台运行
        chrome_options.add_argument("--disable-gpu")
        # 减少内存占用和共享内存问题
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--no-sandbox")
        # 忽略证书错误
        chrome_options.add_argument("--ignore-certificate-errors")

        # headless 支持（在 monitor 容器中通常启用）
        headless_env = os.environ.get("HEADLESS", "true").lower()
        if headless_env in ("1", "true", "yes"):
            # 使用新的 headless 模式（Chrome 109+ 支持）
            try:
                chrome_options.add_argument("--headless=new")
            except Exception:
                chrome_options.add_argument("--headless")

        # 可选：使用持久化 Chrome profile 来保存登录状态（比单独注入 cookies 更稳健）
        use_profile = os.environ.get("USE_CHROME_PROFILE", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        if use_profile:
            profile_dir = os.environ.get("CHROME_PROFILE_DIR", "./data/chrome_profile")
            try:
                chrome_options.add_argument(
                    f"--user-data-dir={Path(profile_dir).as_posix()}"
                )
                logger.info(f"启用 Chrome profile：{profile_dir}")
            except Exception as e:
                logger.warning(f"启用 Chrome profile 失败: {e}")
        # 如果容器里装了 chromium，指定二进制路径以避免找不到浏览器
        for bin_path in (
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome-stable",
        ):
            if Path(bin_path).exists():
                try:
                    chrome_options.binary_location = bin_path
                    logger.info(f"使用浏览器二进制: {bin_path}")
                    break
                except Exception:
                    pass

        # 初始化浏览器
        # 注意：需要安装 Chrome 浏览器和对应版本的 ChromeDriver，或者安装 selenium>=4.6.0 自动管理
        logger.info("启动浏览器...")
        driver = webdriver.Chrome(options=chrome_options)

        # 初始化后尝试加载 cookies（若存在），以恢复登录状态；若未加载再访问目标页面
        try:
            loaded = load_cookies(driver, TARGET_URL)
        except Exception:
            loaded = False

        if not loaded:
            logger.info(f"未找到或无法加载 cookies，访问页面: {TARGET_URL}")
            driver.get(TARGET_URL)

        # 等待页面加载
        time.sleep(5)

        # 处理登录流程
        handle_login_process(driver)

        # === 验证登录结果 ===
        logger.info("等待页面跳转以验证登录...")
        time.sleep(5)

        current_url = driver.current_url
        page_title = driver.title
        logger.info(f"当前页面URL: {current_url}")
        logger.info(f"当前页面标题: {page_title}")

        # driver.save_screenshot("login_debug.png")
        # logger.info("已保存页面截图到 login_debug.png")

        if "passport.nankai.edu.cn" in current_url:
            logger.warning("警告: URL仍包含 passport，可能未跳转")

        if driver.find_elements(By.ID, "password_account_input"):
            logger.error("错误: 仍检测到登录框，登录失败")
        else:
            logger.info("登录框已消失，登录流程已完成")
            # 登录成功后保存 cookies 以便下次复用
            try:
                save_cookies(driver)
            except Exception:
                logger.debug("保存 cookies 时发生异常，已忽略")
        # ===================

        # 导航到目标场馆
        navigate_to_venue(driver)

        # 按日期轮询检查
        return check_dates_availability(driver)

    except Exception as e:
        logger.error(f"检查页面失败: {e}")
        return False, f"检查出错: {e}"
    finally:
        # if driver:
        #     try:
        #         driver.quit()
        #     except:
        #         return True, "未发现'已满'标记，可能有名额！"
        print("结束检查")


def check_time_availability():
    """检查当前时间是否在允许预约的时间范围内"""
    now = get_beijing_time()
    hour = now.hour

    begin_hour = int(os.environ.get("BEGIN_HOUR", 8))
    end_hour = int(os.environ.get("END_HOUR", 21))

    # 允许预约的时间段：早上6点到晚上10点
    if begin_hour <= hour < end_hour:
        return True
    else:
        return False


def main():
    time_state = check_time_availability()
    if not time_state:
        logger.info("当前时间不在允许预约的时间范围内，跳过本次检查")
        return

    logger.info("开始监控预约页面...")
    webhooks = get_webhooks()

    if not webhooks["feishu_group"] and not webhooks["wework"]:
        logger.warning("未配置飞书或企业微信Webhook，仅在控制台输出结果")

    # while True:
    try:
        is_available, message = check_availability()
        print(is_available, message)

    except KeyboardInterrupt:
        logger.info("停止监控")
        # break
    except Exception as e:
        logger.error(f"运行出错: {e}")

    if is_available:
        # 将消息按行拆分为单条场地描述
        lines = [ln.strip() for ln in str(message).splitlines() if ln.strip()]
        include_morning = os.environ.get("INCLUDE_MORNING", "false").lower() == "true"

        # 删除上午数据（若配置为不包含上午），再在所有场地信息中寻找同一天连续 >=2 小时的时段
        filtered_lines = [ln for ln in lines if include_morning or "上午" not in ln]

        # 合并为连续时段（按时间连续，不要求同一场地），只保留 >=2 小时的段
        if LEAST_TIME_LENGTH >= 2:
            merged = build_continuous_periods(
                filtered_lines, include_morning=include_morning
            )
        else:
            merged = filtered_lines

        if not merged:
            logger.info("未找到任何同一天连续 >=2 小时的空余时段，跳过通知")
        else:
            # 过滤已经达到提醒阈值的时段
            try:
                to_notify, _ = filter_messages_by_memory(merged)
            except Exception as e:
                logger.warning(f"过滤提醒记忆时出错，将直接发送全部消息: {e}")
                to_notify = merged

            if not to_notify:
                logger.info("所有发现的连续时段均被记忆阈值抑制，今日不再发送通知")
            else:
                new_message = "\n".join(to_notify)
                report_data = {"message": new_message}
                if webhooks["feishu_person"]:
                    send_to_feishu(
                        webhooks["feishu_person"],
                        report_data,
                        report_type="text",
                        rich_text=False,
                    )
                if webhooks["feishu_group"]:
                    send_to_feishu(
                        webhooks["feishu_group"],
                        report_data,
                        report_type="text",
                    )
                if webhooks["wework"]:
                    send_wework(
                        webhooks["wework"],
                        title="羽毛球场地有名额！",
                        content=new_message,
                        url=TARGET_URL,
                    )


if __name__ == "__main__":
    main()
