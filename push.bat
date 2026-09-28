@echo off
:backend
docker push %ACCOUNT_ID%.dkr.ecr.%REGION%.amazonaws.com/docqa-backend:1
if errorlevel 1 (
  timeout /t 5 /nobreak >nul
  goto backend
)
:frontend
docker push %ACCOUNT_ID%.dkr.ecr.%REGION%.amazonaws.com/docqa-frontend:1
if errorlevel 1 (
  timeout /t 5 /nobreak >nul
  goto frontend
)
echo ALL DONE