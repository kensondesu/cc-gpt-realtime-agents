
FROM python:3.11-bullseye AS runtime
WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --upgrade pip \
	&& pip install -r requirements.txt

COPY . ./audio_backend

RUN if [ ! -d "audio_backend/frontend_dist" ]; then \
		echo "[ERROR] Missing frontend_dist assets." >&2; \
		echo "        Build the frontend (npm run build) and copy the dist output into audio_backend/frontend_dist before building the image." >&2; \
		exit 1; \
	fi

EXPOSE 8080

CMD ["uvicorn", "audio_backend.backend:app", "--host", "0.0.0.0", "--port", "8080"]
