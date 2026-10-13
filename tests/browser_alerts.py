"""Alert settings QA using temporary accounts and a local recording sender."""
import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from alerts import AlertService
from auth import AuthStore
from server import Monitor, make_handler


def main():
    with tempfile.TemporaryDirectory(prefix="monilite-alert-browser-") as directory:
        auth = AuthStore(directory)
        admin, _ = auth.setup("alert_owner", "alert-owner-password")
        auth.create_user(admin["user"]["id"], "alert_member", "alert-member-password", "viewer")
        delivered = []
        alerts = AlertService(auth, sender=lambda config, event: delivered.append(event))
        monitor = Monitor()
        http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor, auth, alerts=alerts))
        monitor.start()
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get("MONILITE_TEST_BROWSER"), args=["--no-sandbox"])
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                base = f"http://127.0.0.1:{http.server_port}"
                page.goto(base)
                page.locator('#auth-username').fill('alert_owner')
                page.locator('#auth-password').fill('alert-owner-password')
                page.locator('#auth-form button[type="submit"]').click()
                expect(page.locator('.metric-card')).to_have_count(4)
                expect(page.locator('a[data-page="alerts"]')).to_be_visible()
                page.locator('a[data-page="settings"]').click()
                expect(page.locator('[data-settings-tab="alerts"]')).to_have_count(0)
                page.locator('[data-settings-tab="account"]').click()
                page.locator('#current-password').fill('incorrect-password')
                page.locator('#new-password').fill('alert-new-password')
                page.locator('#confirm-new-password').fill('alert-new-password')
                page.locator('#password-change-form button[type="submit"]').click()
                expect(page.locator('#password-error')).to_be_visible()
                page.locator('a[data-page="alerts"]').click()
                expect(page.locator('#alert-channel')).to_be_enabled()
                expect(page).to_have_url(base + '/#alerts')
                page.locator('#alert-channel').select_option('webhook')
                page.locator('#alert-credential').fill('https://example.com/notify?key=browser-secret')
                page.locator('#alert-duration').fill('30')
                page.locator('#alert-repeat').fill('600')
                page.locator('#alert-cpu-threshold').fill('85')
                page.locator('#alert-enabled').check()
                page.locator('#alert-save').click()
                expect(page.locator('#alert-credential-status')).to_contain_text('已配置')
                expect(page.locator('#alert-credential')).to_have_value('')
                page.locator('#alert-test').click()
                expect(page.locator('#alert-feedback')).to_contain_text('测试通知发送成功')
                expect(page.locator('#alert-history')).to_contain_text('发送成功')
                assert delivered[-1]['kind'] == 'test'
                assert 'browser-secret' not in page.locator('#page-content').inner_text()
                assert 'browser-secret' not in page.evaluate('JSON.stringify(localStorage)')
                page.reload()
                expect(page.locator('#page-title')).to_contain_text('告警通知')
                expect(page.locator('#alert-duration')).to_have_value('30')
                expect(page.locator('#alert-cpu-threshold')).to_have_value('85')
                expect(page.locator('#alert-enabled')).to_be_checked()
                page.locator('#alert-duration').fill('45')
                page.locator('#alert-save').click()
                expect(page.locator('#alert-feedback')).to_contain_text('配置已保存')
                assert alerts.status()['config']['credential_configured']
                page.locator('#alert-channel').select_option('serverchan')
                expect(page.locator('#alert-test')).to_be_disabled()
                expect(page.locator('#alert-channel-help')).to_contain_text('SendKey')
                page.locator('#alert-channel').select_option('webhook')
                page.locator('#alert-save').click()
                expect(page.locator('#alert-test')).to_be_enabled()
                missing_requests = []
                def missing_once(route):
                    if not missing_requests:
                        missing_requests.append(route.request.url)
                        route.fulfill(status=404, json={"error": "Not found"})
                    else:
                        route.continue_()
                page.route('**/api/admin/alerts', missing_once)
                page.route('**/api/metrics?*', lambda route: route.fulfill(status=503, json={"error": "metrics unavailable"}))
                page.reload()
                expect(page.locator('#alert-feedback')).to_contain_text('重启')
                expect(page.locator('#alert-retry')).to_be_visible()
                expect(page.locator('#alert-channel')).to_be_hidden()
                page.locator('#alert-retry').click()
                expect(page.locator('#alert-channel')).to_be_enabled()
                expect(page.locator('#alert-duration')).to_have_value('45')
                assert page.evaluate('state.data') is None, 'Alert configuration should work without monitoring data'
                page.locator('#alert-duration').fill('46')
                page.unroute('**/api/admin/alerts')
                page.unroute('**/api/metrics?*')
                page.evaluate('fetchData()')
                expect(page.locator('#alert-duration')).to_have_value('46')
                page.locator('#alert-duration').fill('45')
                page.locator('#alert-save').click()
                expect(page.locator('#alert-test')).to_be_enabled()
                page.locator('#toast').evaluate('element => element.hidden=true')
                artifacts = ROOT / '.runtime'
                artifacts.mkdir(exist_ok=True)
                page.screenshot(path=str(artifacts / 'alerts-desktop.png'), full_page=True, animations='disabled')
                for width in (850, 390):
                    page.set_viewport_size({"width": width, "height": 1000})
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), f'overflow at {width}'
                page.screenshot(path=str(artifacts / 'alerts-mobile.png'), full_page=True, animations='disabled')
                page.evaluate("document.body.classList.add('dark')")
                page.screenshot(path=str(artifacts / 'alerts-dark.png'), full_page=True, animations='disabled')
                page.locator('#alert-enabled').uncheck()
                page.locator('#alert-clear-credential').check()
                page.locator('#alert-save').click()
                expect(page.locator('#alert-credential-status')).to_contain_text('未配置')
                expect(page.locator('#alert-test')).to_be_disabled()
                page.set_viewport_size({"width": 1440, "height": 1100})
                held_requests = []
                page.route('**/api/admin/alerts?delayed=1', lambda route: held_requests.append(route))
                page.evaluate("() => {window.oldAlertRequest=apiRequest('/api/admin/alerts?delayed=1').catch(()=>null);}")
                page.locator('#logout-button').click()
                page.locator('#auth-username').fill('alert_member')
                page.locator('#auth-password').fill('alert-member-password')
                page.locator('#auth-form button[type="submit"]').click()
                expect(page.locator('#profile-username')).to_have_text('alert_member')
                expect(page.locator('[data-settings-tab="alerts"]')).to_have_count(0)
                expect(page.locator('a[data-page="alerts"]')).to_be_hidden()
                assert page.evaluate("fetch('/api/admin/alerts').then(r=>r.status)") == 403
                page.evaluate("location.hash='alerts'")
                expect(page.locator('.metric-card')).to_have_count(4)
                expect(page.locator('#alert-form')).to_have_count(0)
                assert not errors, errors
                # A delayed 401 from the old admin session must not expire the member session.
                assert len(held_requests) == 1
                held_requests[0].fulfill(status=401, json={"error": "旧会话已失效"})
                page.evaluate('async () => { await window.oldAlertRequest; }')
                assert page.evaluate('state.user && state.user.username') == 'alert_member'
                expect(page.locator('#auth-form')).to_have_count(0)
                browser.close()
                print('Alert browser checks passed: save/reload, credential redaction, test delivery, clear, member permissions, mobile and dark layouts.')
        finally:
            monitor.stop_event.set()
            http.shutdown()
            http.server_close()
            thread.join()


if __name__ == '__main__':
    main()
