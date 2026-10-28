"""Capture high-resolution screenshots and an end-to-end video of the RIA application flow."""
import shutil
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ART_DIR = Path(r"C:\Users\nvishwakarma\.gemini\antigravity\brain\11390f70-5297-4397-a0ec-850308b761c5")
WS_DIR = Path(r"c:\Users\nvishwakarma\Downloads\1\requirement-intelligence-assistant-co\docs")

ART_IMG = ART_DIR / "screenshots"
ART_VID = ART_DIR / "videos"
WS_IMG = WS_DIR / "screenshots"
WS_VID = WS_DIR / "videos"

for d in (ART_IMG, ART_VID, WS_IMG, WS_VID):
    d.mkdir(parents=True, exist_ok=True)

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE_URL = "http://localhost:8501"
SESSION_ID = "23e4a0abe2bf"


def save_screenshot(page, filename: str, full_page: bool = False):
    art_path = ART_IMG / filename
    page.screenshot(path=str(art_path), full_page=full_page)
    ws_path = WS_IMG / filename
    shutil.copy2(art_path, ws_path)
    print(f"Captured: {filename}")


def run_walkthrough():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME_PATH, headless=True)
        # Record video with 1280x720 resolution
        context = browser.new_context(
            viewport={"width": 1366, "height": 820},
            record_video_dir=str(ART_VID),
            record_video_size={"width": 1280, "height": 720},
        )
        page = context.new_page()

        print("--- Step 1: Home Dashboard ---")
        page.goto(f"{BASE_URL}/")
        page.wait_for_timeout(3000)
        save_screenshot(page, "01_home_dashboard.png")
        page.wait_for_timeout(1500)

        print("--- Step 2: Prepare & Run (Documents) ---")
        page.goto(f"{BASE_URL}/Prepare_and_Run?s={SESSION_ID}")
        page.wait_for_timeout(3000)
        # Scroll slightly to view uploaded documents table
        page.evaluate("window.scrollTo(0, 180)")
        page.wait_for_timeout(1500)
        save_screenshot(page, "02_prepare_and_run_documents.png")

        print("--- Step 3: Prepare & Run (AI Analysis Trigger) ---")
        # Scroll down to Analysis Options & Run button
        page.evaluate("window.scrollTo(0, 520)")
        page.wait_for_timeout(1500)
        save_screenshot(page, "03_prepare_and_run_analysis_options.png")
        page.wait_for_timeout(1500)

        print("--- Step 4: Review & Analysis (Status Overview & Table) ---")
        page.goto(f"{BASE_URL}/Review_and_Analysis?s={SESSION_ID}")
        page.wait_for_timeout(3000)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1000)
        save_screenshot(page, "04_review_and_analysis_overview.png")

        print("--- Step 5: Review & Analysis (Email Draft & Conflicts) ---")
        # Click on "Draft clarification email" button
        try:
            email_btn = page.locator("button:has-text('Draft clarification email')").first
            if email_btn.is_visible():
                email_btn.click()
                page.wait_for_timeout(2500)
                # Scroll down to see the email draft panel and the Conflicting Requirements table
                page.evaluate("window.scrollTo(0, 320)")
                page.wait_for_timeout(1500)
                save_screenshot(page, "05_review_and_analysis_email_draft.png")
        except Exception as e:
            print("Email draft click note:", e)

        print("--- Step 6: Stories & Tests (Story Board & 4-section layout) ---")
        page.goto(f"{BASE_URL}/Stories_and_Tests?s={SESSION_ID}")
        page.wait_for_timeout(3000)
        # Expand Acceptance Requirement & QA tests expander if available
        try:
            expander = page.locator("details summary:has-text('Acceptance Requirement')").first
            if expander.is_visible():
                expander.click()
                page.wait_for_timeout(1500)
        except Exception as e:
            print("Expander click note:", e)
        page.evaluate("window.scrollTo(0, 100)")
        page.wait_for_timeout(1500)
        save_screenshot(page, "06_stories_and_tests_board.png")

        print("--- Step 7: Stories & Tests (Jira Push Tab) ---")
        try:
            jira_tab = page.locator("button[role='tab']:has-text('Jira push')").first
            if jira_tab.is_visible():
                jira_tab.click()
                page.wait_for_timeout(2000)
                save_screenshot(page, "07_stories_and_tests_jira.png")
        except Exception as e:
            print("Jira tab click note:", e)
        page.wait_for_timeout(1500)

        print("--- Step 8: Export Center ---")
        page.goto(f"{BASE_URL}/Export?s={SESSION_ID}")
        page.wait_for_timeout(3000)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1500)
        save_screenshot(page, "08_export_center.png")
        page.wait_for_timeout(2000)

        # Close page and context to ensure video is finalized on disk
        video_obj = page.video
        page.close()
        context.close()
        browser.close()

        if video_obj:
            video_path = video_obj.path()
            print("Playwright recorded raw video at:", video_path)
            target_art = ART_VID / "ria_walkthrough_video.webm"
            target_ws = WS_VID / "ria_walkthrough_video.webm"
            shutil.copy2(video_path, target_art)
            shutil.copy2(video_path, target_ws)
            print(f"Video saved as: {target_art}")
            print(f"Workspace video saved as: {target_ws}")


if __name__ == "__main__":
    run_walkthrough()
