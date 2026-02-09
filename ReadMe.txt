1.以管理员身份打开 PowerShell：
	# 安装 Python（如果未安装）
	winget install Python.Python.3

	# 安装 paramiko（SSH 库）
	pip install paramiko

2.运行程序
	python gms_gui.py

3.创建桌面快捷键
	PS C:\Users\hcq> cd D:\鑫森淼焱垚095\9-Tools\GMS_Auto_Test\
	PS D:\鑫森淼焱垚095\9-Tools\GMS_Auto_Test> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
	PS D:\鑫森淼焱垚095\9-Tools\GMS_Auto_Test> .\create_shortcut.ps1
	
4.生成应用程序
	PS D:\鑫森淼焱垚095\9-Tools\GMS_Auto_Test> pip install pyinstaller
	PS D:\鑫森淼焱垚095\9-Tools\GMS_Auto_Test> python build_app.py