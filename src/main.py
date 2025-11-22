from fastapi import FastAPI
from .routers import webhook

app = FastAPI()

app.include_router(webhook.router)

@app.get("/")
async def read_root():
    return {"Hello": "World"}