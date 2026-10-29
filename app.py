from fastapi import FastAPI
from invoice_agent import router

app = FastAPI()
app.include_router(router)
