@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo.
echo CTBRec Mobile Reviewer 2.10 - copy settings/state from an older package
echo -------------------------------------------------------------------
echo This copies configuration, durable queues, Recu state, and useful caches.
echo It DOES NOT copy Python/JS/code files from the old version.
echo.
set "OLD="
set /p "OLD=Paste the full path to your OLD CTBRec_Mobile_Reviewer folder: "
set "OLD=%OLD:"=%"
if not exist "%OLD%\mobile_config.json" (
  echo.
  echo ERROR: mobile_config.json was not found in:
  echo   %OLD%
  echo Nothing was copied.
  pause
  exit /b 1
)

echo.
echo Copying state from:
echo   %OLD%
echo To:
echo   %CD%
echo.

for %%F in (
  mobile_config.json
  recording_roots.txt
  keeplasts.txt
  mobile_recu_session.json
  mobile_action_queue.json
  mobile_hidden_models.json
  mobile_catalog_cache.json
  mobile_ready_work_index.json
  mobile_offline_sync_history.json
  mobile_library_mosaic_state.json
  mobile_non_nsfw_index.json
  mobile_mosaic_layout_cache.json
  mosaic_lite_duration_cache.json
  mosaic_lite_model_names.json
  mosaic_manifest.json
  mosaic_lite_recu_cache.json
  mosaic_lite_transcript_cache.json
  review_sort_duration_cache.json
  review_model_size_cache.json
  review_mosaic_manifest.json
  mosaic_lite_settings.json
  review_sort_lite_settings.json
) do (
  if exist "%OLD%\%%F" (
    copy /Y "%OLD%\%%F" "%CD%\%%F" >nul
    echo   copied %%F
  )
)

if exist "%OLD%\recu_browser_profile" (
  if not exist "%CD%\recu_browser_profile" mkdir "%CD%\recu_browser_profile" >nul 2>&1
  xcopy /E /I /Y /Q "%OLD%\recu_browser_profile\*" "%CD%\recu_browser_profile\" >nul
  echo   copied recu_browser_profile\
)


if not exist "%CD%\models" mkdir "%CD%\models" >nul 2>&1
if exist "%CD%\models\nudenet_640m.onnx" (
  echo   keeping bundled models\nudenet_640m.onnx
) else if exist "%OLD%\models\nudenet_640m.onnx" (
  copy /Y "%OLD%\models\nudenet_640m.onnx" "%CD%\models\nudenet_640m.onnx" >nul
  echo   copied models\nudenet_640m.onnx ^(validated on first use^)
) else if exist "%OLD%\models\640m.onnx" (
  copy /Y "%OLD%\models\640m.onnx" "%CD%\models\nudenet_640m.onnx" >nul
  echo   copied models\640m.onnx as models\nudenet_640m.onnx
) else if exist "%OLD%\640m.onnx" (
  copy /Y "%OLD%\640m.onnx" "%CD%\models\nudenet_640m.onnx" >nul
  echo   copied 640m.onnx as models\nudenet_640m.onnx
) else if exist "%OLD%\nudenet_640m.onnx" (
  copy /Y "%OLD%\nudenet_640m.onnx" "%CD%\models\nudenet_640m.onnx" >nul
  echo   copied nudenet_640m.onnx into models\
)

if exist "%OLD%\mobile_delete_mosaics" (
  if not exist "%CD%\mobile_delete_mosaics" mkdir "%CD%\mobile_delete_mosaics" >nul 2>&1
  xcopy /E /I /Y /Q "%OLD%\mobile_delete_mosaics\*" "%CD%\mobile_delete_mosaics\" >nul
  echo   copied mobile_delete_mosaics\
)

echo.
echo Done. IMPORTANT: Run this only while the OLD server is stopped.
echo Next: run install_mobile_reviewer.bat, then start_mobile_reviewer.bat.
echo.
pause
