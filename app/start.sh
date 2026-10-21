#!/bin/sh
pip install --no-cache-dir fastapi==0.115.0 uvicorn[standard]==0.30.6 jinja2==3.1.4 pillow==10.4.0 aiofiles==24.1.0 pyjwt==2.9.0 python-multipart==0.0.12
exec uvicorn app.main:app --host 0.0.0.0 --port 8000