; TokenSafe Windows installer (Inno Setup 6).
;
; The .exe bundles the engine tree for one git tag and hands the runtime work to
; installer\bootstrap.ps1, so a machine with neither CPython nor Node still ends
; up with a working `tokensafe` command (the pinned CPython is downloaded from
; bin\python-runtime.json and verified by SHA-256).
;
; Built and, when signing credentials are configured, signed by
; .github/workflows/installer.yml on windows-latest:
;   ISCC.exe /DAppVersion=<version> /DSourceRoot=<repo> /DOutputDir=<out> installer\tokensafe.iss
;
; Install scope: per-user, no UAC (PrivilegesRequired=lowest), so the PATH entry
; lives in HKCU\Environment and is removed again on uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef SourceRoot
  #define SourceRoot ".."
#endif
#ifndef OutputDir
  #define OutputDir "output"
#endif

[Setup]
AppId={{8E3B1F2C-6A4D-4E59-9C2B-7F1A5D3C9E60}
AppName=TokenSafe
AppVersion={#AppVersion}
AppVerName=TokenSafe {#AppVersion}
AppPublisher=TokenSafe
AppPublisherURL=https://tokensafe.dev
AppSupportURL=https://tokensafe.dev/docs
AppUpdatesURL=https://tokensafe.dev/docs
DefaultDirName={autopf}\TokenSafe
DefaultGroupName=TokenSafe
DisableProgramGroupPage=yes
LicenseFile={#SourceRoot}\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=TokenSafe-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Per-user install: no elevation prompt, PATH goes to HKCU\Environment.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
UninstallDisplayName=TokenSafe
; The tree carries ~40 MB of python sources and docs; the installer itself stays
; around that size, the 47 MB runtime is downloaded on first install.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: ".git\*, .github\*, tests\*, node_modules\*, dist\*, dist-installer\*, installer-output\*, tokensafe-out\*, data\*, __pycache__\*, *.pyc"

[Registry]
; Append-only and idempotent: NeedsAddPath keeps re-installs from duplicating the
; entry, expandsz keeps %USERPROFILE% and friends expandable in the user PATH.
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}\bin"; Check: NeedsAddPath('{app}\bin')

[Run]
; Agent wiring and login are interactive (they may start a proxy, install the UI
; companion and open a browser), so they run from the Finish page in a visible
; console. A hidden, unattended `setup --all` can block forever on a prompt -
; that is exactly what a silent install must never do.
Filename: "{cmd}"; Parameters: "/c ""{app}\bin\tokensafe.cmd"" setup --all & echo. & pause"; \
    Description: "Configure the AI agents found on this machine"; \
    Flags: postinstall skipifsilent
Filename: "{cmd}"; Parameters: "/c ""{app}\bin\tokensafe.cmd"" login"; \
    Description: "Log in to activate the free Community license"; \
    Flags: postinstall nowait skipifsilent

[Code]
const
  BootstrapRelative = 'installer\bootstrap.ps1';

function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
  Entry: string;
begin
  Entry := ExpandConstant(Param);
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Entry) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

function RemovePathEntry(OrigPath, Entry: string): string;
var
  Haystack, Needle: string;
  Position: Integer;
begin
  Haystack := ';' + Uppercase(OrigPath) + ';';
  Needle := ';' + Uppercase(Entry) + ';';
  Position := Pos(Needle, Haystack);
  if Position > 0 then
  begin
    Delete(Haystack, Position, Length(Needle) - 1);
    Haystack := Copy(Haystack, 2, Length(Haystack) - 2);
  end
  else
  begin
    Haystack := OrigPath;
  end;
  Result := Haystack;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  PowerShell, Params: string;
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    exit;

  PowerShell := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{app}\' + BootstrapRelative) + '" -AppDir "' + ExpandConstant('{app}') +
    '" -Mode Provision';

  if (not Exec(PowerShell, Params, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode))
     or (ResultCode <> 0) then
    MsgBox('TokenSafe was installed, but the runtime setup did not finish.' + #13#10 + #13#10 +
      'The log is at %USERPROFILE%\.tokensafe\install.log' + #13#10 + #13#10 +
      'Retry from a terminal with:' + #13#10 +
      '"' + ExpandConstant('{app}\bin\tokensafe.cmd') + '" setup --all',
      mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  CurrentPath, Entry: string;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;

  Entry := ExpandConstant('{app}\bin');
  if RegQueryStringValue(HKCU, 'Environment', 'Path', CurrentPath) then
    RegWriteExpandStringValue(HKCU, 'Environment', 'Path', RemovePathEntry(CurrentPath, Entry));
end;
