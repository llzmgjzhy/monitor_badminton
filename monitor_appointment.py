# coding=utf-8
import os
import time
import requests
import yaml
import logging
from datetime import datetime
from pathlib import Path
import pytz
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# 新增 Selenium 相关库
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# === 配置日志 ===
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# === 配置部分 (请在此处修改目标URL和判断逻辑) ===
TARGET_URL = "https://tyggl.nankai.edu.cn/Views/User/User.html"  # 目标页面URL
CHECK_INTERVAL = 60  # 检查间隔(秒)

# 判断逻辑配置
# 如果页面包含此关键词，表示有空余 (例如: "可预约", "Available", "有号")
SUCCESS_KEYWORDS = ["可预约", "有号", "Available"]
# 如果页面包含此关键词，表示已满 (例如: "已满", "Sold Out")
FAILURE_KEYWORDS = ["已满", "Sold Out", "暂无"]

# === 核心功能 ===


def get_check_days_count():
    """
    根据当前时间确定需要监控的天数
    规则: 18:00之前只能预定今天及之后两天(共3天)，18:00之后可以预定今天及之后三天(共4天)
    """
    now = datetime.now(pytz.timezone("Asia/Shanghai"))
    if now.hour < 18:
        return 3
    else:
        return 4


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

    feishu_url = os.environ.get("FEISHU_WEBHOOK_URL") or webhooks.get("feishu_url")
    wework_url = os.environ.get("WEWORK_WEBHOOK_URL") or webhooks.get("wework_url")

    return {"feishu": feishu_url, "wework": wework_url}


def send_feishu(webhook_url, title, content, url=None):
    """发送飞书通知"""
    if not webhook_url:
        return

    headers = {"Content-Type": "application/json"}

    text_content = f"{title}\n\n{content}"
    if url:
        text_content += f"\n\n链接: {url}"

    payload = {"msg_type": "text", "content": {"text": text_content}}

    try:
        response = requests.post(webhook_url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200 and response.json().get("code") == 0:
            logger.info("飞书通知发送成功")
        else:
            logger.error(f"飞书通知发送失败: {response.text}")
    except Exception as e:
        logger.error(f"发送飞书通知出错: {e}")


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

        # 尝试点击协议勾选框 (根据用户提供的元素特征)
        try:
            # 查找 id="iconxy" 的 i 标签
            agreement_checkbox = driver.find_elements(By.ID, "iconxy")
            if agreement_checkbox:
                logger.info("找到协议勾选框(id='iconxy')，正在点击...")
                agreement_checkbox[0].click()
                time.sleep(1)
            else:
                # 备用：通过 label 文本查找前一个 i 标签
                logger.info("未通过ID找到勾选框，尝试通过文本定位...")
                agreement_checkbox = driver.find_elements(
                    By.XPATH,
                    "//label[contains(text(), '我已阅读并同意')]/preceding-sibling::i",
                )
                if agreement_checkbox:
                    agreement_checkbox[0].click()
                    time.sleep(1)
        except Exception as e:
            logger.warning(f"点击协议勾选框时出错: {e}")

        # 点击'下一步'按钮
        try:
            # 优先尝试通过 ID 查找 (根据用户提供的元素特征 id="apay")
            next_button = driver.find_elements(By.ID, "apay")
            if next_button:
                logger.info("找到'下一步'按钮(id='apay')，正在点击...")
                next_button[0].click()
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
        chrome_options.add_argument("--no-sandbox")
        # 忽略证书错误
        chrome_options.add_argument("--ignore-certificate-errors")

        # 初始化浏览器
        # 注意：需要安装 Chrome 浏览器和对应版本的 ChromeDriver，或者安装 selenium>=4.6.0 自动管理
        logger.info("启动浏览器...")
        driver = webdriver.Chrome(options=chrome_options)

        logger.info(f"正在访问页面: {TARGET_URL}")
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


def main():
    logger.info("开始监控预约页面...")
    webhooks = get_webhooks()

    if not webhooks["feishu"] and not webhooks["wework"]:
        logger.warning("未配置飞书或企业微信Webhook，仅在控制台输出结果")

    last_success_time = 0
    notification_interval = 3600  # 成功后每小时提醒一次，避免轰炸

    # while True:
    try:
        is_available, message = check_availability()
        print(is_available, message)

        # if is_available:
        #     logger.info(f"【好消息】{message}")

        #     current_time = time.time()
        #     # 控制发送频率
        #     # if current_time - last_success_time > notification_interval:
        #     #     title = "🎉 发现预约名额"
        #     #     content = f"检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n状态: {message}"

        #     #     if webhooks["feishu"]:
        #     #         send_feishu(webhooks["feishu"], title, content, TARGET_URL)

        #     #     if webhooks["wework"]:
        #     #         send_wework(webhooks["wework"], title, content, TARGET_URL)

        #     #     last_success_time = current_time
        # else:
        #     logger.info(f"【监控中】{message}")

    except KeyboardInterrupt:
        logger.info("停止监控")
        # break
    except Exception as e:
        logger.error(f"运行出错: {e}")

        # time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
