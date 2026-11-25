"""可选页面冒烟检查：先启动服务，再 pip install playwright，使用已安装的 Edge。"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    output = Path(__file__).resolve().parents[1] / "data"
    output.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8000", wait_until="networkidle")
        page.get_by_role("button", name="查看补货建议").click()
        page.locator("#reviewer").wait_for(timeout=90000)
        assert page.locator("#report").inner_text().find("168") >= 0
        assert "先卖库存，暂停补货" in page.locator("#report").inner_text()
        assert "先核实，暂不生成补货数量" in page.locator("#report").inner_text()
        assert page.locator("#agent-steps .agent-step").count() == 6
        assert "销售分析" in page.locator("#agent-steps").inner_text()
        page.locator("#agent-steps details").evaluate_all("items => items.forEach(item => item.open = true)")
        assert "查询：销售记录汇总" in page.locator("#agent-steps").inner_text()
        page.locator("details").evaluate_all("items => items.forEach(item => item.open = true)")
        for raw in ["SKU-", "WH-", "demo", "other", "Agent", "Supervisor", "商策"]:
            assert raw not in page.locator("body").inner_text(), raw
        page.locator("details").evaluate_all("items => items.forEach(item => item.open = false)")
        page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
        page.screenshot(path=str(output / "demo-desktop.png"), full_page=True)
        page.locator("#reviewer").fill("页面演示审核员")
        page.locator("#qty-2").fill("0")
        page.locator("#reason-2").fill("等待供应商确认，暂不采购")
        page.locator("#review-note").fill("已核对库存差异，缺失供应信息的商品暂缓采购")
        page.locator("#ack").check()
        page.get_by_role("button", name="确认数量并保存建议").click()
        page.get_by_role("link", name="下载中文报告").wait_for(timeout=90000)
        assert "已确认并保存建议" in page.locator("#report").inner_text()
        with page.expect_download() as download_info:
            page.get_by_role("link", name="下载中文报告").click()
        download = download_info.value
        assert download.suggested_filename == "补货建议与审核记录.html"
        html = Path(download.path()).read_text(encoding="utf-8")
        assert "挂耳咖啡" in html and "人工审核记录" in html and "SKU-" not in html
        page.reload(wait_until="networkidle")
        page.get_by_role("link", name="下载中文报告").wait_for(timeout=30000)
        page.set_viewport_size({"width":390,"height":844})
        page.screenshot(path=str(output / "demo-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "页面横向溢出"
        page.locator('[name=scenario][value=promotion]').check()
        assert page.locator("#factor-label").is_visible()
        page.get_by_role("button", name="查看补货建议").click()
        page.locator("#reviewer").wait_for(timeout=90000)
        assert "建议补 288 盒" in page.locator("#report").inner_text()
        assert not errors, errors
        print(json.dumps({"browser":"Edge", "analysis":"passed", "review":"passed", "reload":"passed", "mobile":"passed", "console_errors":errors}, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
