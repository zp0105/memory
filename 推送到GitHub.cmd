@echo off
rem ============================================================
rem  把「回忆」推到你的 GitHub
rem
rem  用之前先在网页上建一个空仓库：
rem     github.com/new  ->  仓库名 huiyi  ->  什么都别勾  ->  Create
rem  然后双击本脚本，按提示输入你的 GitHub 用户名。
rem ============================================================
setlocal
cd /d "D:\DHS\回忆"

echo.
echo   ================ 推送到 GitHub ================
echo.
echo   前提：你已经在 github.com/new 建好了名为 huiyi 的空仓库
echo         （不要勾选 Add a README / .gitignore / license）
echo.

set /p GHUSER=  你的 GitHub 用户名:
if "%GHUSER%"=="" (echo   没输入用户名，退出 & pause & exit /b 2)

git remote remove origin 2>nul
git remote add origin https://github.com/%GHUSER%/huiyi.git
git branch -M main

echo.
echo   正在推送...（第一次会让你登录 GitHub）
echo   GitHub 现在不接受账号密码，密码处要填 Personal Access Token
echo     token 在： github.com/settings/tokens  ->  Generate new token (classic)
echo     勾选 repo 权限即可
echo.

git push -u origin main
if %ERRORLEVEL%==0 (
  echo.
  echo   ✓ 推送成功！打开看看： https://github.com/%GHUSER%/huiyi
) else (
  echo.
  echo   ✗ 推送失败。常见原因：
  echo     1. 仓库没建 / 名字不是 huiyi
  echo     2. 认证失败 —— 密码处要填 token，不是登录密码
  echo     3. 网络问题（可以试试开代理，或改用 SSH）
)
echo.
pause
