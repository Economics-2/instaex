@echo off
rem Insta.exe লোকাল কম্পিউটারে বানানোর স্ক্রিপ্ট (GitHub Actions ব্যবহার করলে এটি লাগবে না)
rem আগে bin\ ফোল্ডারে yt-dlp.exe ও ffmpeg.exe রাখুন।
if not exist bin\yt-dlp.exe ( echo bin\yt-dlp.exe নেই & exit /b 1 )
if not exist bin\ffmpeg.exe ( echo bin\ffmpeg.exe নেই & exit /b 1 )

pip install -r requirements.txt || exit /b 1
pyinstaller --noconfirm --clean --onefile --noconsole --name Insta ^
  --icon assets\icon.ico --version-file version_info.txt ^
  --add-data "web;web" --add-data "bin;bin" --add-data "assets\icon.ico;assets" --add-data "extension;extension" ^
  --collect-all webview ^
  main.py || exit /b 1

echo.
echo তৈরি হয়েছে: dist\Insta.exe
