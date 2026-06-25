FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY . /app

RUN mkdir -p /app/quant_wechat_bot/.cache

EXPOSE 8790

CMD ["sh", "-lc", "python3 -m quant_wechat_bot.bot_service serve --host 0.0.0.0 --port ${PORT:-8790}"]
