from aiohttp import web
import json


async def crocopay_callback(request):
    try:
        data = await request.json()

        print("=== CROCOPAY CALLBACK ===")
        print(json.dumps(data, indent=4, ensure_ascii=False))

        return web.json_response({
            "success": True
        })

    except Exception as e:
        print(f"CROCOPAY CALLBACK ERROR: {e}")

        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


app = web.Application()

app.router.add_post(
    "/crocopay/callback",
    crocopay_callback
)

if __name__ == "__main__":
    web.run_app(
        app,
        host="127.0.0.1",
        port=8083
    )
