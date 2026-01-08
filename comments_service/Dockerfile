FROM python:3.9-slim
WORKDIR /app
RUN pip install flask flask-cors
COPY . .
EXPOSE 5001
CMD ["python", "comments.py"]