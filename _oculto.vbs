' Sobe um comando SEM abrir janela nenhuma.
'
' Existe porque o Windows nao tem jeito nativo de rodar um .bat invisivel:
' "start /min" ainda deixa um item na barra de tarefas, e usar pythonw.exe
' direto resolveria a janela mas perderia o log (pythonw nao tem stdout pra
' redirecionar). Entao o .bat continua sendo o supervisor, com o log inteiro,
' e este .vbs so o lanca escondido.
'
' Uso:  wscript.exe _oculto.vbs "C:\...\bot.bat" rodar
'
' Nao clique duas vezes aqui - use o bot.bat.

Option Explicit

Dim shell, args, comando, i

Set shell = CreateObject("WScript.Shell")
Set args = WScript.Arguments

If args.Count = 0 Then
    MsgBox "Este arquivo e um auxiliar do bot.bat e nao deve ser aberto direto." & vbCrLf & vbCrLf & _
           "Abra o bot.bat.", vbInformation, "Super Odds Scanner"
    WScript.Quit 1
End If

comando = """" & args(0) & """"
For i = 1 To args.Count - 1
    comando = comando & " " & args(i)
Next

' 0 = janela oculta, False = nao espera terminar.
shell.Run comando, 0, False
