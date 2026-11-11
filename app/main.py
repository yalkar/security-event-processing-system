from fastapi import FastAPI

app = FastAPI(title="SEPS Event API", version="0.1.0")


@app.get("/")
def hello_world() -> dict[str, str]:
    return {"message": "Hello World"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
