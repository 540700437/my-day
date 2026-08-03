"""
知乎问题回答抓取器（CDP 连接版 v8）
核心改动：不再用 launch_persistent_context 启动新 Edge（会被强制加上
--no-sandbox 等自动化标志，导致知乎直接拦截），改为 connect_over_cdp
需要连接手动打开、已登录好的 Edge 浏览器
"""

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
from dataclasses import dataclass, field
from typing import List
import time
import random
import re
import sqlite3
import hashlib
import json
import zlib
import contextlib


# ==================== 配置区（按需修改） ====================
URL_QUESTION = "https://www.zhihu.com/question/28966967"
DB_PATH = "zhihu_answers.db"
LOGIN_COOKIE_NAME = "z_c0"
COOKIE_CHECK_URL = "https://www.zhihu.com"

# 默认不存完整富文本 HTML，避免数据库膨胀；设为 True 则存压缩后的 HTML
STORE_CONTENT_HTML = False
CONTENT_HTML_MAX_CHARS = 20000

# 知乎顶部 sticky AppHeader 大致高度（px），用于点击前二次校正滚动位置
STICKY_HEADER_OFFSET = 80

# CDP 连接地址（默认 9222，如果你改了快捷方式里的端口，这里也要改）
CDP_URL = "http://localhost:9222"
# ==========================================================


SELECTORS = {
    "question_title": "h1.QuestionHeader-title",
    "answer_item": ".List-item",
    "rich_content": ".RichContent-inner",
    "expand_button": ".ContentItem-expandButton",
    "author_name": ".AuthorInfo-name .UserLink-link",
    "vote_count": ".VoteButton--up",
    "answer_time": ".ContentItem-time a",
}

# 按优先级排列："加载更多"按钮的候选选择器
LOAD_MORE_SELECTOR_CANDIDATES = [
    "[data-za-detail-view-element_name='MoreAnswers']",
    "[data-za-detail-view-element_name='QuestionAnswerLoadMore']",
    "[data-za-detail-view-element_name*='More' i]",
    "button[class*='more' i]",
    "a[class*='more' i]",
    "span[class*='more' i]",
    "button:has-text('更多回答')",
    "button:has-text('查看全部')",
    "button:has-text('继续浏览')",
    "button:has-text('展开更多')",
    "a:has-text('更多回答')",
    "a:has-text('展开更多')",
    "span:has-text('更多回答')",
]

EXPAND_MARKER_ATTR = "data-scraper-expanded"
EXPAND_ATTEMPTS_ATTR = "data-scraper-attempts"
MAX_EXPAND_ATTEMPTS = 3


@dataclass
class Answer:
    dedup_key: str
    author: str
    vote_text: str
    vote_count: int
    time: str
    link: str
    content: str
    content_html: str = ""
    images: List[str] = field(default_factory=list)
    code_blocks: List[str] = field(default_factory=list)
    has_video: bool = False


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def random_sleep(a=0.6, b=1.5):
    time.sleep(random.uniform(a, b))


def human_like_scroll(page, swipes=(1, 2)):
    """模拟真人触屏惯性滑动：多小步 + 速度按摩擦系数衰减，先快后慢"""
    for _ in range(random.randint(*swipes)):
        remaining = random.randint(500, 1200)
        velocity = random.uniform(45, 75)
        friction = random.uniform(0.85, 0.92)

        while remaining > 5 and velocity > 2:
            step = min(velocity, remaining)
            page.evaluate("(y) => window.scrollBy(0, y)", step)
            remaining -= step
            velocity *= friction
            time.sleep(random.uniform(0.02, 0.05))

        if remaining > 0:
            page.evaluate("(y) => window.scrollBy(0, y)", remaining)

        random_sleep(0.2, 0.6)


def clean_vote_count(text):
    """统一清洗赞同数为整数"""
    if not text:
        return 0
    text = text.replace("赞同", "").replace(",", "").strip()
    if not text:
        return 0
    match = re.match(r"([\d.]+)\s*(万|w)?", text, re.IGNORECASE)
    if not match:
        return 0
    num_str, unit = match.group(1), match.group(2)
    try:
        num = float(num_str)
    except ValueError:
        return 0
    if unit:
        num *= 10000
    return int(num)


def make_dedup_key(link, author, content):
    if link:
        return link
    raw = f"{author}-{content[:80]}"
    return "hash:" + hashlib.md5(raw.encode("utf-8")).hexdigest()


def compress_html(html: str) -> bytes:
    """截断 + zlib 压缩，控制 SQLite 体积"""
    if not html:
        return b""
    if len(html) > CONTENT_HTML_MAX_CHARS:
        html = html[:CONTENT_HTML_MAX_CHARS] + "...[TRUNCATED]"
    return zlib.compress(html.encode("utf-8"), level=6)


# ---------------------------------------------------------------------------
# 登录态检测
# ---------------------------------------------------------------------------

def has_login_cookie(context):
    for c in context.cookies(COOKIE_CHECK_URL):
        if c.get("name") == LOGIN_COOKIE_NAME and c.get("value"):
            return True
    return False


def ensure_logged_in(context, timeout_wait=180):
    if has_login_cookie(context):
        print(f"✅ 检测到登录态（{LOGIN_COOKIE_NAME} cookie 存在）")
        return

    print(f"⚠️ 未检测到登录态 cookie（{LOGIN_COOKIE_NAME}），"
          f"知乎游客态下部分回答会被折叠/限制。")
    print(f"   请在已打开的 Edge 浏览器窗口中手动登录知乎，"
          f"最多等待 {timeout_wait} 秒...")

    start = time.time()
    while time.time() - start < timeout_wait:
        if has_login_cookie(context):
            print(f"✅ 已检测到登录成功（{LOGIN_COOKIE_NAME} cookie 出现），继续抓取")
            return
        time.sleep(2)

    print("⚠️ 等待登录超时，将以当前状态（可能是游客态）继续抓取")


# ---------------------------------------------------------------------------
# 展开"阅读全部"
# ---------------------------------------------------------------------------

def expand_new_answers(page):
    handles = page.query_selector_all(
        f"{SELECTORS['expand_button']}:not([{EXPAND_MARKER_ATTR}])"
    )
    success_count = 0
    fail_count = 0

    for handle in handles:
        try:
            handle.scroll_into_view_if_needed(timeout=1500)
            page.evaluate(
                "(offset) => window.scrollBy(0, -offset)",
                STICKY_HEADER_OFFSET,
            )
        except Exception:
            pass

        clicked = False
        try:
            handle.click(timeout=1500)
            clicked = True
            success_count += 1
        except Exception:
            fail_count += 1

        if clicked:
            try:
                handle.evaluate(
                    f"el => el.setAttribute('{EXPAND_MARKER_ATTR}', '1')"
                )
            except Exception:
                pass
            continue

        # 失败则记录尝试次数
        try:
            attempts = handle.evaluate(
                f"""el => {{
                    const n = parseInt(el.getAttribute('{EXPAND_ATTEMPTS_ATTR}') || '0', 10) + 1;
                    el.setAttribute('{EXPAND_ATTEMPTS_ATTR}', String(n));
                    return n;
                }}"""
            )
        except Exception:
            attempts = MAX_EXPAND_ATTEMPTS

        if attempts >= MAX_EXPAND_ATTEMPTS:
            try:
                handle.evaluate(
                    f"el => el.setAttribute('{EXPAND_MARKER_ATTR}', '1')"
                )
            except Exception:
                pass

    if fail_count:
        print(
            f"  ⚠️ 本轮展开按钮：成功 {success_count}，失败 {fail_count}"
            f"（未达 {MAX_EXPAND_ATTEMPTS} 次上限的会在后续轮次重试）"
        )

    return success_count + fail_count


def click_load_more_if_present(page):
    """按优先级依次尝试候选选择器"""
    for sel in LOAD_MORE_SELECTOR_CANDIDATES:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                continue
            btn.scroll_into_view_if_needed(timeout=1500)
            page.evaluate(
                "(offset) => window.scrollBy(0, -offset)",
                STICKY_HEADER_OFFSET,
            )
            btn.click(timeout=2000)
            print(f"  ➡️ 点击了「加载更多」按钮（匹配规则: {sel}）")
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# 滚动加载
# ---------------------------------------------------------------------------

def scroll_to_load_more(page, max_answers=20, max_rounds=25):
    print(f"🔄 开始滚动加载，目标至少 {max_answers} 个回答...")

    def get_state():
        return page.evaluate(
            """(sel) => ({
                count: document.querySelectorAll(sel).length,
                height: document.body.scrollHeight
            })""",
            SELECTORS["rich_content"],
        )

    state = get_state()
    last_count, last_height = state["count"], state["height"]
    no_change_rounds = 0
    expand_new_answers(page)

    for i in range(max_rounds):
        human_like_scroll(page)

        try:
            page.wait_for_function(
                """(args) => document.querySelectorAll(args.sel).length > args.prev""",
                arg={"sel": SELECTORS["rich_content"], "prev": last_count},
                timeout=4000,
            )
        except PWTimeoutError:
            pass

        expand_new_answers(page)

        state = get_state()
        current_count, current_height = state["count"], state["height"]
        print(f"  第 {i + 1} 次滚动，当前回答数：{current_count}")

        if current_count >= max_answers:
            print(f"✅ 已加载 {current_count} 个回答，满足目标")
            break

        if current_count == last_count and current_height == last_height:
            no_change_rounds += 1
            if no_change_rounds >= 3:
                if click_load_more_if_present(page):
                    no_change_rounds = 0
                    random_sleep(1.0, 2.0)
                    expand_new_answers(page)
                    state = get_state()
                    last_count, last_height = state["count"], state["height"]
                    continue
                print("⚠️ 连续多轮数量和页面高度均无变化，且无更多加载按钮，判定已到底")
                break
        else:
            no_change_rounds = 0

        last_count, last_height = current_count, current_height

    expand_new_answers(page)


# ---------------------------------------------------------------------------
# 提取回答
# ---------------------------------------------------------------------------

def extract_answers(page, want_html: bool = STORE_CONTENT_HTML) -> List[Answer]:
    raw = page.evaluate(
        """(sel) => {
            const items = document.querySelectorAll(sel.item);
            const results = [];
            items.forEach(item => {
                const contentEl = item.querySelector(sel.content);
                if (!contentEl) return;
                const content = contentEl.innerText.trim();
                if (!content) return;

                const authorEl = item.querySelector(sel.author);
                const author = authorEl ? authorEl.innerText.trim() : '匿名用户';

                const voteEl = item.querySelector(sel.vote);
                const voteText = voteEl ? voteEl.innerText.trim() : '';

                const timeEl = item.querySelector(sel.time);
                const timeText = timeEl ? timeEl.innerText.trim() : '';
                let link = timeEl ? (timeEl.getAttribute('href') || '') : '';
                if (link.startsWith('//')) link = 'https:' + link;

                const images = Array.from(contentEl.querySelectorAll('img'))
                    .map(img => img.getAttribute('src') || img.getAttribute('data-original') || '')
                    .filter(Boolean);

                const codeBlocks = Array.from(contentEl.querySelectorAll('pre'))
                    .filter(pre => !pre.closest('blockquote'))
                    .map(pre => {
                        const codeEl = pre.querySelector('code');
                        return (codeEl ? codeEl.innerText : pre.innerText).trim();
                    })
                    .filter(Boolean);

                const hasVideo = contentEl.querySelector(
                    'video, .VideoAnswerPlayer, .RichText-video'
                ) !== null;

                const contentHtml = sel.wantHtml ? contentEl.innerHTML : '';

                results.push({
                    author, voteText, timeText, link, content,
                    contentHtml, images, codeBlocks, hasVideo,
                });
            });
            return results;
        }""",
        {
            "item": SELECTORS["answer_item"],
            "content": SELECTORS["rich_content"],
            "author": SELECTORS["author_name"],
            "vote": SELECTORS["vote_count"],
            "time": SELECTORS["answer_time"],
            "wantHtml": want_html,
        },
    )

    print(f"\n📊 浏览器端一次性提取到 {len(raw)} 条候选回答")

    results: List[Answer] = []
    for row in raw:
        dedup_key = make_dedup_key(row["link"], row["author"], row["content"])
        results.append(
            Answer(
                dedup_key=dedup_key,
                author=row["author"],
                vote_text=row["voteText"],
                vote_count=clean_vote_count(row["voteText"]),
                time=row["timeText"],
                link=row["link"],
                content=row["content"],
                content_html=row["contentHtml"],
                images=row["images"],
                code_blocks=row["codeBlocks"],
                has_video=row["hasVideo"],
            )
        )

    return results


# ---------------------------------------------------------------------------
# SQLite 存储
# ---------------------------------------------------------------------------

def init_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            question_url TEXT NOT NULL,
            dedup_key TEXT NOT NULL,
            author TEXT,
            vote_text TEXT,
            vote_count INTEGER,
            answer_time TEXT,
            link TEXT,
            content TEXT,
            content_html_gz BLOB,
            images TEXT,
            code_blocks TEXT,
            has_video INTEGER,
            scraped_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(question_url, dedup_key)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_answers_question
        ON answers(question_url)
        """
    )
    conn.commit()


def save_answers(conn, question: str, question_url: str, answers: List[Answer]):
    cur = conn.cursor()
    inserted = 0
    for ans in answers:
        cur.execute(
            """
            INSERT OR IGNORE INTO answers
                (question, question_url, dedup_key, author, vote_text,
                 vote_count, answer_time, link, content, content_html_gz,
                 images, code_blocks, has_video)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question,
                question_url,
                ans.dedup_key,
                ans.author,
                ans.vote_text,
                ans.vote_count,
                ans.time,
                ans.link,
                ans.content,
                compress_html(ans.content_html),
                json.dumps(ans.images, ensure_ascii=False),
                json.dumps(ans.code_blocks, ensure_ascii=False),
                int(ans.has_video),
            ),
        )
        if cur.rowcount:
            inserted += 1
    conn.commit()
    total_in_db = cur.execute(
        "SELECT COUNT(*) FROM answers WHERE question_url = ?", (question_url,)
    ).fetchone()[0]
    return inserted, total_in_db


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def goto_with_retry(page, url, retries=3, timeout=60000):
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, timeout=timeout)
            return
        except PWTimeoutError:
            print(f"⚠️ 页面加载超时，第 {attempt}/{retries} 次重试...")
            random_sleep(2, 4)
    raise RuntimeError(f"多次重试后仍无法打开页面：{url}")


def run_scrape(conn, context, page):
    print(f"🌐 正在打开问题页：{URL_QUESTION}")
    goto_with_retry(page, URL_QUESTION)

    page.wait_for_selector(SELECTORS["question_title"], timeout=15000)
    random_sleep(1.5, 2.5)

    ensure_logged_in(context)

    title = page.locator(SELECTORS["question_title"]).inner_text()
    print(f"\n📌 问题：{title}")
    print(f"🔗 当前URL：{page.url}\n")

    scroll_to_load_more(page, max_answers=20, max_rounds=25)
    answers = extract_answers(page)

    for idx, ans in enumerate(answers, 1):
        preview = ans.content.replace("\n", " ")[:150]
        extras = []
        if ans.images:
            extras.append(f"图片x{len(ans.images)}")
        if ans.code_blocks:
            extras.append(f"代码块x{len(ans.code_blocks)}")
        if ans.has_video:
            extras.append("含视频")
        extra_note = f" [{', '.join(extras)}]" if extras else ""
        print(
            f"--- 回答 {idx} | {ans.author} | 赞同 {ans.vote_count}{extra_note} ---"
        )
        print(f"{preview}...\n")

    inserted, total_in_db = save_answers(conn, title, page.url, answers)
    print(
        f"💾 本次新增 {inserted} 条（数据库层面自动去重），"
        f"该问题在库中累计 {total_in_db} 条 -> {DB_PATH}"
    )


def scrape_zhihu_question():
    with contextlib.closing(
        sqlite3.connect(DB_PATH, check_same_thread=False)
    ) as conn:
        init_db(conn)

        with sync_playwright() as p:
            # ========== 核心改动：连接已打开的 Edge，而不是启动新的 ==========
            print(f"🔌 正在连接 Edge（{CDP_URL}）...")
            browser = p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0]

# 优先复用已打开的问题页，没有就新建
target_page = None
for p in context.pages:
    if "question/28966967" in p.url:
        target_page = p
        print(f"♻️ 复用已打开的标签页: {p.url}")
        break

page = target_page if target_page else context.new_page()


            try:
                run_scrape(conn, context, page)
            except PWTimeoutError as e:
                print(f"❌ 超时错误：{e}")
                page.screenshot(path="error_timeout.png")
                print("📸 已截图保存：error_timeout.png")
            except Exception as e:
                print(f"❌ 出错：{type(e).__name__}: {e}")
                page.screenshot(path="error.png")
                print("📸 已截图保存：error.png")
            finally:
                input("\n⏸️ 按 Enter 关闭当前标签页...")
                page.close()
                # 注意：不要 browser.close()！Edge 保持运行，下次还能连


if __name__ == "__main__":
    scrape_zhihu_question()
