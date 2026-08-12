' Lanzador de Blip sin ventana de consola.
' Doble clic en este fichero para arrancar la app.
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "D:\DEV\blip"
' El "0" oculta cualquier ventana de consola; False = no esperar.
shell.Run "pythonw.exe app.py", 0, False
