; 安装/卸载时的自定义 NSIS 脚本。
; electron-builder 默认就会 include build/installer.nsh，不用额外配置。
;
; 干什么：
;   1. 把 chaierww-backend.exe 也收掉（见下方 preInit / customUnInit 的说明）
;   2. 卸载前弹一次「数据不会被删」的提示，允许取消（customUnInit 开头）
;   3. 安装完成页写清「常驻托盘 / 数据位置」（MUI_FINISHPAGE_TEXT）
;
; 注意：本文件被前插在 !include MUI2.nsh 之前，只能用 NSIS 核心指令
; （IfSilent / IntCmp / MessageBox / StrCpy…），不能用 LogicLib 的 ${If} 语法。
;
; 为什么收后端：NSIS 默认的「先关掉正在运行的应用」只认 APP_EXECUTABLE_FILENAME
; （chaierww.exe），完全不认识后端进程。而后端是 PyInstaller --onefile 打的
; （引导器 + 子进程两层），旧版被 /f 强杀时子进程会变孤儿，继续锁着
; resources/backend/chaierww-backend.exe。安装程序解压新包时 7z 覆盖不了这个
; 被锁的文件，就弹「chaierww 无法关闭」，而它的重试只查 chaierww.exe（早没了），
; 于是永远重试不过去 —— 重装就这么卡死。
;
; preInit 在 .onInit 最前面执行，早于 NSIS 的进程检查和解压。
;
; 两个分支不一样：
;   - 安装：只在「主程序没在跑、后端却在跑」时动手。这组合就是旧版留下的孤儿。
;     主程序正常开着的话交给默认的「请关闭 chaierww」流程，不该提前把它的后端打死。
;   - 卸载：应用本来就该关掉，直接收。

; 安装完成页：用 !define 而不是自己写 customFinishPage 宏——后者会整段替换模板里的
; 完成页、连带丢掉 StartApp 函数定义，升级 electron-builder 就会炸。
!ifndef ONE_CLICK
  !define MUI_FINISHPAGE_TEXT "chaierww 已安装，会常驻系统托盘。$\r$\n关闭窗口只是收进托盘，退出请用托盘右键 →「退出」。$\r$\n$\r$\n数据保存在 $PROFILE\.chaierww，卸载不会删除。$\r$\n详见安装目录下的 INSTALL.txt。"
  !define MUI_WELCOMEPAGE_TITLE "欢迎使用 chaierww"
  !define MUI_WELCOMEPAGE_TEXT "把 WorkBuddy / Trae 的账号收在一处，一键切号、签到积分、用量统计。$\r$\n$\r$\n默认只为当前用户安装，不需要管理员权限。"
!endif

!macro preInit
  ; tasklist 加 /FI 没匹配到时退出码仍是 0，所以拿 find 的退出码判断
  ; （找到 → 0，没找到 → 1）。
  nsExec::ExecToStack '"$SYSDIR\cmd.exe" /c tasklist /FI "IMAGENAME eq chaierww.exe" /NH | "$SYSDIR\find.exe" /I "chaierww.exe"'
  Pop $0
  Pop $1
  IntCmp $0 0 preInit_done preInit_check_orphan preInit_check_orphan

  preInit_check_orphan:
    nsExec::ExecToStack '"$SYSDIR\cmd.exe" /c tasklist /FI "IMAGENAME eq chaierww-backend.exe" /NH | "$SYSDIR\find.exe" /I "chaierww-backend.exe"'
    Pop $2
    Pop $3
    IntCmp $2 0 preInit_kill preInit_done preInit_done

  preInit_kill:
    DetailPrint "清理旧版残留的后端进程…"
    nsExec::Exec '"$SYSDIR\cmd.exe" /c taskkill /F /IM "chaierww-backend.exe"'
    Pop $4
    Sleep 600

  preInit_done:
!macroend

!macro customUnInit
  ; 卸载前先说明「数据不会被删」，并允许取消。
  ;
  ; 关键：必须用 IfSilent（核心指令）而不是 LogicLib 的 ${IfNot} ${Silent}——
  ; 本文件被 electron-builder 前插在 !include MUI2.nsh 之前，此时 LogicLib 还没进来，
  ; 用 ${IfNot} 会直接编译失败。静默卸载（electron-updater 自动更新带 /S）绝不能弹框，
  ; 否则升级会永久卡在后端卸载这一步。
  IfSilent cw_un_continue 0
  MessageBox MB_OKCANCEL|MB_ICONINFORMATION "卸载只删除程序文件、快捷方式和开始菜单项。$\r$\n$\r$\n账号、凭据、备份、日志与网页版登录态都在 $PROFILE\.chaierww，会被保留，重装后旧数据会回来。要彻底清干净，卸载后手动删除该目录。$\r$\n$\r$\n更新偏好（含「跳过的版本」）在 $APPDATA\chaierww。$\r$\n$\r$\n继续卸载？" /SD IDOK IDOK cw_un_continue
  Quit
  cw_un_continue:

  ; —— 以下为原有「收掉后端进程」逻辑，原样保留 ——
  nsExec::Exec '"$SYSDIR\cmd.exe" /c taskkill /F /IM "chaierww-backend.exe"'
  Pop $0
  Sleep 400
!macroend
