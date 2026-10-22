; 安装/卸载时的自定义 NSIS 脚本。
; electron-builder 默认就会 include build/installer.nsh，不用额外配置。
;
; 干什么：把 chaierww-backend.exe 也收掉。
;
; 为什么：NSIS 默认的「先关掉正在运行的应用」只认 APP_EXECUTABLE_FILENAME
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
  nsExec::Exec '"$SYSDIR\cmd.exe" /c taskkill /F /IM "chaierww-backend.exe"'
  Pop $0
  Sleep 400
!macroend
