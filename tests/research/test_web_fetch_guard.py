"""web._fetch_one must refuse non-public targets, including via redirects (issue #31)."""
import httpx

from engram.research.web import _fetch_one

PUBLIC = "93.184.216.34"


async def test_fetch_one_refuses_private_url():
    def handler(request):
        return httpx.Response(200, text="leaked")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _fetch_one(client, "http://127.0.0.1:8080/") == ""


async def test_fetch_one_refuses_redirect_to_private():
    def handler(request):
        if request.url.host == PUBLIC:
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9999/secret"})
        return httpx.Response(200, text="leaked")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _fetch_one(client, f"http://{PUBLIC}/") == ""


async def test_fetch_one_still_fetches_public_text():
    def handler(request):
        return httpx.Response(200, text="hello world")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _fetch_one(client, f"http://{PUBLIC}/page") == "hello world"
