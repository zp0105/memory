@echo off
rem ============================================================
rem  把「回忆 / HuiYi」推到你的 GitHub
rem
rem  用之前先在网页上建一个空仓库（建议先私有）：
rem     github.com/new
rem       仓库名：huiyi
rem       Visibility：Private        <- 先私有！确认没问题再改 Public
rem       什么都别勾（README / .gitignore / license 都不要）
rem       Create
rem  然后双击本脚本，按提示输入你的 GitHub 用户名。
rem ============================================================
setlocal
cd /d "D:\DHS\回忆"

echo.
echo   ================ 推送到 GitHub ================
echo.
echo   前提：已在 github.com/new 建好空仓库
echo         名字 huiyi，Visibility 选 Private，其余都不勾
echo.

set /p GHUSER=  你的 GitHub 用户名:
if "%GHUSER%"=="" (echo   没输入用户名，退出 & pause & exit /b 2)

git remote remove origin 2>nul
git remote add origin https://github.com/%GHUSER%/huiyi.git
git branch -M main

echo.
echo   正在推送...（第一次会让你登录）
echo   GitHub 已不接受账号密码 —— 密码处要填 Personal Access Token
echo     建 token： github.com/settings/tokens
echo               Generate new token (classic) -> 勾 repo -> 生成
echo.

git push -u origin main
if %ERRORLEVEL%==0 (
  echo.
  echo   ============================================================
  echo    OK 推送成功
  echo   ============================================================
  echo.
  echo   去看看： https://github.com/%GHUSER%/huiyi
  echo.
  echo   确认没问题之后，再公开发布：
  echo     Settings（仓库设置）-> 拉到最下面 Danger Zone
  echo     -> Change repository visibility -> Make public
  echo.
  echo   （那个 token 用完可以随时删：github.com/settings/tokens）
) else (
  echo.
  echo   ============================================================
  echo    X 推送失败，常见原因：
  echo   ============================================================
  echo     1. 仓库没建，或者名字不是 huiyi
  echo     2. 认证失败 —— 密码处必须填 token，不是 GitHub 登录密码
  echo     3. 网络问题（国内访问 GitHub 常需要代理；也可以改用 SSH）
  echo.
  echo   把这里的报错发我，我帮你看。
)
echo.
pause
