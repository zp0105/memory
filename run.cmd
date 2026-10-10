@echo off
rem 语音链路统一入口：配好沙箱内可写的工作区环境后调用指定 python 脚本
setlocal
set "PY=%LOCALAPPDATA%\dsh-py\python.exe"
set "ROOT=D:\DHS"
set "LIBS=%ROOT%\.cache\pylibs"
set "PYTHONPATH=%LIBS%;%ROOT%\voice\CosyVoice-src;%ROOT%\voice\CosyVoice-src\third_party\Matcha-TTS"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "TMP=%ROOT%\.cache\tmp"
set "TEMP=%ROOT%\.cache\tmp"
set "TMPDIR=%ROOT%\.cache\tmp"
set "MPLCONFIGDIR=%ROOT%\.cache\mpl"
set "MODELSCOPE_CACHE=%ROOT%\.cache\modelscope"
set "HF_HOME=%ROOT%\.cache\hf"
set "TORCH_HOME=%ROOT%\.cache\torch"
set "DSH_SKIP_TF_VERSION_CHECK=1"
set "KMP_DUPLICATE_LIB_OK=TRUE"
if not exist "%ROOT%\.cache\tmp" mkdir "%ROOT%\.cache\tmp"
if not exist "%ROOT%\.cache\mpl" mkdir "%ROOT%\.cache\mpl"
if "%~1"=="" (
  echo usage: run.cmd ^<script.py^> [args...]
  exit /b 2
)
"%PY%" "%~1" %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%
