; 安装/卸载时的自定义 NSIS 脚本。
; electron-builder 默认就会 include build/installer.nsh，不用额外配置。
;
; 干什么：
;   1. 把 chaierww-backend.exe 也收掉（见下方 preInit / customUnInit 的说明）
;   2. 卸载前弹一次「数据不会被删」的提示，允许取消（customUnInit 开头）
;   3. 定各个向导页的文案（欢迎页 / 目录页 / 完成页），并把欢迎页补回来
;   4. 跳过「为所有用户 / 仅为我安装」那一页（customInstallMode）
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

; ——— 向导各页文案 ———
;
; 这些不是 MUI 的宏而是「默认值」：MUI2 内部是
; `!insertmacro MUI_DEFAULT MUI_WELCOMEPAGE_TEXT "$(MUI_TEXT_WELCOME_INFO_TEXT)"`，
; 只要在 include MUI2 之前先 !define，MUI_DEFAULT 就不会覆盖。所以既不用自己写
; customWelcomePage / customFinishPage 宏（那会整段替换模板、连带丢掉 StartApp，
; 升级 electron-builder 就炸），也不用担心被模板改回去。
;
; 语言（简体中文）在 package.json 的 build.nsis.language 里定，这里只管正文。
!ifndef ONE_CLICK
  ; 欢迎页：一次说清「装什么 / 装在哪 / 数据在哪 / 要不要管理员」。
  !define MUI_WELCOMEPAGE_TITLE "安装 chaierww"
  !define MUI_WELCOMEPAGE_TEXT "把 WorkBuddy / Trae 的多个账号收在一处：一键切号、签到积分、账号体检、用量统计。$\r$\n$\r$\n只为当前用户安装，不需要管理员权限，也不写系统目录。$\r$\n账号、凭据、备份与日志都在 $PROFILE\.chaierww —— 升级和卸载都不会动它。"

  ; 目录页：用户最容易把「安装目录」当成「数据目录」，这里当场分开讲。
  !define MUI_DIRECTORYPAGE_TEXT_TOP "程序文件装到这里（约 130 MB）。$\r$\n账号数据不在这里，在 $PROFILE\.chaierww；换安装位置不影响已有账号。"
  !define MUI_DIRECTORYPAGE_TEXT_DESTINATION "安装到："

  ; 完成页：默认文本区只有 40u（约 3 行），长文案会被裁掉，所以打开 TEXT_LARGE（60u）。
  !define MUI_FINISHPAGE_TEXT_LARGE
  !define MUI_FINISHPAGE_TEXT "chaierww 已安装，并常驻系统托盘。$\r$\n关闭窗口只是收进托盘，退出请用托盘右键 →「退出」。$\r$\n$\r$\n账号与凭据保存在 $PROFILE\.chaierww，卸载不会删除。$\r$\n详见安装目录下的 INSTALL.txt 与 DISCLAIMER.md。"
  !define MUI_FINISHPAGE_RUN_TEXT "立即运行 chaierww"
  ; 完成页底部的小链接（点击时按运行期展开，所以 $INSTDIR 指向真实安装位置）。
  !define MUI_FINISHPAGE_LINK "打开使用说明（INSTALL.txt）"
  !define MUI_FINISHPAGE_LINK_LOCATION "$INSTDIR\INSTALL.txt"
!endif

; 跳过「Choose Installation Options / 为所有用户还是仅为我」这一页。
;
; 本软件把程序装到当前用户目录、注册表只写 HKCU（perMachine 未开），
; 「为所有用户安装」对它没有实际意义，却会多一次 UAC 提权弹窗和一次选择。
; electron-builder 的 multiUserUi.nsh 里有 `!ifmacrodef customInstallMode` 钩子，
; 把 $isForceCurrentInstall 置 1 后该页在 PRE 回调里直接 Abort（等于跳过），
; 并按当前用户安装。这也是官方文档给 per-user 应用的标准做法。
!macro customInstallMode
  StrCpy $isForceCurrentInstall "1"
!macroend

; 让欢迎页真的出现。
;
; electron-builder 的 assisted installer 默认**不插** MUI_PAGE_WELCOME（模板里只有
; 许可 / 安装范围 / 目录 / 安装中 / 完成五页），所以 MUI_WELCOMEPAGE_* 这组 define
; 在没插页之前是不生效的死配置 —— 用户打开安装包第一屏直接是一大段许可条款。
; 这里通过官方的 customWelcomePage 钩子把**标准**欢迎页补回去：只是多插一页，
; 不接管向导流程，因此不会像自定义页面那样一升级 electron-builder 就炸。
!macro customWelcomePage
  !insertmacro MUI_PAGE_WELCOME
!macroend

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
