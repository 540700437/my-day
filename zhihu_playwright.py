from playwright.sync_api import sync_playwright
import time


url = "https://www.zhihu.com"


with sync_playwright() as p:

    browser = p.chromium.launch_persistent_context(
        user_data_dir=r"C:\Users\Lenovo\Desktop\Edge_Playwright",
        channel="msedge",
        headless=False
    )


    page = browser.pages[0] if browser.pages else browser.new_page()


    print("打开知乎首页")

    page.goto(
        url,
        timeout=60000
    )


    time.sleep(15)


    print("首页标题：", page.title())

    print("首页网址：", page.url)


    input("首页正常后按 Enter")


    # 再进入问题页面
    page.goto(
        "https://www.zhihu.com/question/28966967",
        timeout=60000
    )


    time.sleep(10)


    print("问题页面标题：", page.title())

    print(page.locator("body").inner_text()[:500])


    input("结束按 Enter")

    browser.close()