@echo off
REM ============================================================================
REM  Super Odds Scanner - controle do bot no Windows
REM
REM  Clique duas vezes neste arquivo para abrir o menu, ou use pela linha de
REM  comando:
REM
REM      bot.bat iniciar     sobe o bot em segundo plano (sem janela nenhuma)
REM      bot.bat parar       encerra o bot
REM      bot.bat status      diz se esta rodando e mostra o fim do log
REM      bot.bat log         acompanha o log ao vivo (Ctrl+C fecha so o log,
REM                          o bot continua rodando)
REM      bot.bat autostart   sobe junto com o Windows toda vez que voce logar
REM      bot.bat desativar   desliga o inicio automatico
REM
REM  "bot.bat rodar" e uso interno (o supervisor). Nao chame direto.
REM
REM  Nota sobre o PowerShell embutido: nenhum comando aqui usa pipe (^|). Dentro
REM  de aspas o cmd nao processa o acento circunflexo, entao o "^" chegaria
REM  literal no PowerShell e quebraria. Por isso tudo e escrito com foreach em
REM  vez de pipeline - e feio, mas funciona sem escape.
REM ============================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1

set "PROJETO=%~dp0"
set "PROJETO=%PROJETO:~0,-1%"
set "LOGDIR=%PROJETO%\logs"
set "TAREFA=SuperOddsScanner"

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM --- acha o Python ----------------------------------------------------------
REM O python da Microsoft Store fica atras de um alias em WindowsApps, e o
REM caminho real muda de versao pra versao. Perguntar ao proprio interpretador
REM evita fixar um caminho que quebra na proxima atualizacao.
set "PY="
for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
if not defined PY (
    for /f "delims=" %%i in ('py -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
)
if not defined PY (
    echo.
    echo  [ERRO] Python nao encontrado no PATH.
    echo         Instale o Python e marque "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

if "%~1"=="" goto menu
if /i "%~1"=="iniciar"    goto iniciar
if /i "%~1"=="rodar"      goto rodar
if /i "%~1"=="parar"      goto parar
if /i "%~1"=="status"     goto status
if /i "%~1"=="log"        goto log
if /i "%~1"=="autostart"  goto autostart
if /i "%~1"=="desativar"  goto desativar
goto desconhecido

REM ============================================================================
:menu
cls
echo.
echo   ============================================
echo      SUPER ODDS SCANNER
echo   ============================================
echo.
call :estado
if "!RODANDO!"=="1" (
    echo      Estado: RODANDO  ^(PID !BOTPID!^)
) else (
    echo      Estado: parado
)
echo.
echo      [1] Iniciar em segundo plano
echo      [2] Parar
echo      [3] Ver o log ao vivo
echo      [4] Status detalhado
echo      [5] Subir junto com o Windows  ^(ligar^)
echo      [6] Subir junto com o Windows  ^(desligar^)
echo      [0] Sair
echo.
set "ESCOLHA="
set /p "ESCOLHA=  Opcao: "
if "!ESCOLHA!"=="1" (call :fazer_iniciar & pause & goto menu)
if "!ESCOLHA!"=="2" (call :fazer_parar   & pause & goto menu)
if "!ESCOLHA!"=="3" (call :fazer_log     & goto menu)
if "!ESCOLHA!"=="4" (call :fazer_status  & pause & goto menu)
if "!ESCOLHA!"=="5" (call :fazer_autostart & pause & goto menu)
if "!ESCOLHA!"=="6" (call :fazer_desativar & pause & goto menu)
if "!ESCOLHA!"=="0" exit /b 0
goto menu

REM --- pontos de entrada da linha de comando -----------------------------------
:iniciar
call :fazer_iniciar
exit /b 0
:parar
call :fazer_parar
exit /b 0
:status
call :fazer_status
exit /b 0
:log
call :fazer_log
exit /b 0
:autostart
call :fazer_autostart
exit /b 0
:desativar
call :fazer_desativar
exit /b 0

REM ============================================================================
:fazer_iniciar
call :estado
if "!RODANDO!"=="1" (
    echo.
    echo  Ja esta rodando ^(PID !BOTPID!^). Nada a fazer.
    echo.
    goto :eof
)
echo.
echo  Subindo o bot em segundo plano...
start "" wscript.exe "%PROJETO%\_oculto.vbs" "%PROJETO%\bot.bat" rodar
powershell -NoProfile -Command "Start-Sleep -Seconds 5" >nul 2>&1
call :estado
if "!RODANDO!"=="1" (
    echo  OK - rodando ^(PID !BOTPID!^), sem janela.
    echo.
    echo  Logs em: %LOGDIR%
    echo  Acompanhar:  bot.bat log
    echo  Encerrar:    bot.bat parar
) else (
    echo  [ATENCAO] nao consegui confirmar que subiu.
    echo  Veja o fim do log com:  bot.bat status
)
echo.
goto :eof

REM ============================================================================
:rodar
REM Supervisor: mantem o bot de pe. Reiniciar e seguro - todo o estado esta no
REM SQLite: a fila de avaliacao retoma de onde parou e a dedup impede realertar
REM o que ja foi enviado. Ver AVISOS.md secao 7.
:supervisor
for /f "delims=" %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "HOJE=%%d"
set "LOG=%LOGDIR%\bot-!HOJE!.log"
for /f "delims=" %%t in ('powershell -NoProfile -Command "Get-Date -Format \"yyyy-MM-dd HH:mm:ss\""') do set "AGORA=%%t"
echo [!AGORA!] [supervisor] iniciando run.py>>"!LOG!"

"%PY%" -X utf8 "%PROJETO%\run.py" >>"!LOG!" 2>&1
set "SAIDA=!errorlevel!"

for /f "delims=" %%t in ('powershell -NoProfile -Command "Get-Date -Format \"yyyy-MM-dd HH:mm:ss\""') do set "AGORA=%%t"
echo [!AGORA!] [supervisor] run.py saiu com codigo !SAIDA! - reiniciando em 30s>>"!LOG!"
REM A espera nao e cerimonia: sem ela, um erro de configuracao viraria milhares
REM de reinicios por minuto enchendo o disco de log.
powershell -NoProfile -Command "Start-Sleep -Seconds 30" >nul 2>&1
goto supervisor

REM ============================================================================
:fazer_parar
call :estado
if not "!RODANDO!"=="1" (
    echo.
    echo  O bot nao esta rodando.
    echo.
    goto :eof
)
echo.
echo  Encerrando ^(PID !BOTPID!^)...
REM Mata o supervisor junto: parar so o python faria o supervisor subir outro
REM em 30 segundos.
powershell -NoProfile -Command "$a='%PROJETO%'; foreach ($x in Get-CimInstance Win32_Process) { if ($x.CommandLine) { $ehBot = ($x.Name -like 'python*' -and $x.CommandLine -like ('*'+$a+'*run.py*')); $ehSup = ($x.Name -eq 'cmd.exe' -and $x.CommandLine -like '*bot.bat*rodar*'); if ($ehBot -or $ehSup) { Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue } } }"
powershell -NoProfile -Command "Start-Sleep -Seconds 2" >nul 2>&1
call :estado
if "!RODANDO!"=="1" (
    echo  [ATENCAO] ainda aparece rodando ^(PID !BOTPID!^).
) else (
    echo  OK - parado.
)
echo.
goto :eof

REM ============================================================================
:fazer_status
call :estado
echo.
if "!RODANDO!"=="1" (
    echo   Estado    : RODANDO ^(PID !BOTPID!^)
) else (
    echo   Estado    : parado
)
schtasks /Query /TN "%TAREFA%" >nul 2>&1
if errorlevel 1 (
    echo   Autostart : desligado
) else (
    echo   Autostart : LIGADO ^(sobe no logon^)
)
echo   Python    : %PY%
echo   Logs      : %LOGDIR%
echo.
echo   ---- ultimas linhas do log ----
REM -Encoding UTF8: o run.py escreve UTF-8, e sem isto o acento sai como "TÃªnis".
powershell -NoProfile -Command "$fs=@(Get-ChildItem '%LOGDIR%\bot-*.log' -ErrorAction SilentlyContinue); if ($fs.Count) { Get-Content $fs[-1].FullName -Tail 15 -Encoding UTF8 } else { Write-Host '  (nenhum log ainda)' }"
echo.
goto :eof

REM ============================================================================
:fazer_log
echo.
echo  Acompanhando o log. Ctrl+C fecha ESTA janela; o bot continua rodando.
echo.
powershell -NoProfile -Command "$fs=@(Get-ChildItem '%LOGDIR%\bot-*.log' -ErrorAction SilentlyContinue); if ($fs.Count) { Get-Content $fs[-1].FullName -Tail 40 -Wait -Encoding UTF8 } else { Write-Host 'Nenhum log ainda - o bot ja foi iniciado?'; Start-Sleep 4 }"
goto :eof

REM ============================================================================
:fazer_autostart
echo.
echo  Registrando para subir junto com o Windows...
schtasks /Create /TN "%TAREFA%" /TR "wscript.exe \"%PROJETO%\_oculto.vbs\" \"%PROJETO%\bot.bat\" rodar" /SC ONLOGON /RL LIMITED /F >nul 2>&1
if errorlevel 1 (
    echo  [ERRO] nao consegui registrar a tarefa.
    echo         Clique com o botao direito no bot.bat e use
    echo         "Executar como administrador".
) else (
    echo  OK - o bot sobe sozinho toda vez que voce logar no Windows.
    echo  Para desligar:  bot.bat desativar
)
echo.
goto :eof

REM ============================================================================
:fazer_desativar
echo.
schtasks /Delete /TN "%TAREFA%" /F >nul 2>&1
if errorlevel 1 (
    echo  Nao havia inicio automatico registrado.
) else (
    echo  OK - inicio automatico desligado.
    echo  O bot que ja esta rodando continua ate voce dar: bot.bat parar
)
echo.
goto :eof

REM ============================================================================
:estado
REM Define RODANDO=1 e BOTPID quando acha o python DESTE projeto rodando.
REM
REM O nome do processo NAO e "python.exe": o alias da Microsoft Store sobe como
REM "python3.12.exe". Por isso o casamento e por "python*" no nome mais o
REM caminho do projeto na linha de comando. O nome ainda importa - sem ele, o
REM proprio PowerShell desta consulta casaria consigo mesmo, porque o caminho
REM do projeto aparece dentro do comando que ele esta executando.
set "RODANDO=0"
set "BOTPID="
for /f "delims=" %%p in ('powershell -NoProfile -Command "$a='%PROJETO%'; foreach ($x in Get-CimInstance Win32_Process) { if ($x.Name -like 'python*' -and $x.CommandLine -like ('*'+$a+'*run.py*')) { $x.ProcessId; break } }"') do set "BOTPID=%%p"
if defined BOTPID set "RODANDO=1"
goto :eof

REM ============================================================================
:desconhecido
echo.
echo  Comando desconhecido: %~1
echo  Use: iniciar ^| parar ^| status ^| log ^| autostart ^| desativar
echo.
exit /b 1
