"""Launch the Voicebot live server."""
import uvicorn
import config

if __name__ == "__main__":
    uvicorn.run("utils.server_app:app", host=config.SERVER_HOST, port=config.SERVER_PORT)