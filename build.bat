@echo off
REM Builds dist\BatteryWidget.exe. Requires Python 3.8+ installed once (python.org, tick "Add to PATH").
python -m pip install --upgrade pyinstaller || goto :err
python -m PyInstaller --onefile --noconsole --name BatteryWidget battery_widget.py || goto :err
echo.
echo Done: dist\BatteryWidget.exe
pause
exit /b 0
:err
echo Build failed.
pause
exit /b 1
