@echo off
REM Start the dist_mon remote VoIP agent at the far end of a call path.
REM Requires: agent.py + voip_proto.py in this folder. No pip installs.
python agent.py %*
pause
