FROM docker:24.0.5-dind

RUN apk add --no-cache python3 py3-pip bash

WORKDIR /app

COPY . .

RUN pip install fastapi uvicorn pydantic

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]
