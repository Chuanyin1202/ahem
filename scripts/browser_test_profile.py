"""Opt-in local QA: installed Chrome, external font CSS stubbed only in tests.

PYTHONPATH=src:scripts python -m pytest -p browser_test_profile -q tests
No application/auth requests are intercepted; font appearance is not validated.
"""
from playwright.sync_api import BrowserType, Page
from playwright.async_api import BrowserType as AsyncBrowserType, Page as AsyncPage


def pytest_configure(config):
    launch, alaunch = BrowserType.launch, AsyncBrowserType.launch
    goto, agoto = Page.goto, AsyncPage.goto

    def local_launch(self, *args, **kwargs):
        if self.name == 'chromium' and not kwargs.get('channel') and not kwargs.get('executable_path'):
            kwargs['channel'] = 'chrome'
        return launch(self, *args, **kwargs)

    async def local_alaunch(self, *args, **kwargs):
        if self.name == 'chromium' and not kwargs.get('channel') and not kwargs.get('executable_path'):
            kwargs['channel'] = 'chrome'
        return await alaunch(self, *args, **kwargs)

    def local_goto(self, *args, **kwargs):
        self.route('https://fonts.googleapis.com/**', lambda route: route.fulfill(status=200, body='', content_type='text/css'))
        return goto(self, *args, **kwargs)

    async def local_agoto(self, *args, **kwargs):
        async def font_css(route):
            await route.fulfill(status=200, body='', content_type='text/css')
        await self.route('https://fonts.googleapis.com/**', font_css)
        return await agoto(self, *args, **kwargs)

    BrowserType.launch, AsyncBrowserType.launch = local_launch, local_alaunch
    Page.goto, AsyncPage.goto = local_goto, local_agoto
