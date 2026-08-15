@echo off
echo Menginstal PyInstaller...
pip install pyinstaller

echo.
echo Membersihkan build lama...
rmdir /s /q build
rmdir /s /q dist

echo.
echo Memulai proses kompilasi (Single Executable)...
pyinstaller --noconfirm ^
    --onefile ^
    --windowed ^
    --icon "assets/icon.ico" ^
    --name "Horizon" ^
    --add-data "assets;assets/" ^
    --add-data "version.txt;." ^
    --hidden-import "main.discord_services" ^
    --hidden-import "main.scraper_service" ^
    --hidden-import "main.System.monitor" ^
    --exclude-module "pandas" ^
    --exclude-module "numpy" ^
    --exclude-module "matplotlib" ^
    --exclude-module "scipy" ^
    --exclude-module "IPython" ^
    --exclude-module "pytest" ^
    --exclude-module "unittest" ^
    --exclude-module "pydoc" ^
    --exclude-module "notebook" ^
    --exclude-module "pytz" ^
    --exclude-module "sqlite3" ^
    --exclude-module "sqlalchemy" ^
    build.pyw

echo.
echo Selesai! File Horizon.exe dapat ditemukan di folder 'dist'
pause
