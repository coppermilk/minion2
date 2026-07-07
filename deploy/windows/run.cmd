@echo off
rem Double-click this (or point Task Scheduler at it) to start the
rem Windows bots. It runs run.ps1 with the execution policy bypassed
rem for this one process, so it works even when "running scripts is
rem disabled on this system" -- no Set-ExecutionPolicy, nothing to
rem think about. %~dp0 is this file's own folder, so it works from
rem anywhere.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
