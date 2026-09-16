; Inno Setup script -- Windows installer for puretext.
;
; Build order:
;     python packaging\build_standalone.py      -> dist\puretext.exe
;     iscc packaging\puretext.iss               -> Output\puretext-setup-x.y.z.exe
;
; Installs the single executable and puts it on PATH, so `puretext` works from
; any Command Prompt or PowerShell window. Per-user install by default: it needs
; no administrator rights, which is what makes it usable on a locked-down
; corporate machine -- the same audience that cannot pip install anything.

#define AppName "puretext"
#define AppVersion "0.2.0"
#define AppPublisher "Brandon S. Butler"
#define AppURL "https://github.com/brandonsbutler-ai/puretext"
#define AppExeName "puretext.exe"

[Setup]
AppId={{7B3C1E52-9A4D-4C7E-9B2F-6E1D0A5F8C34}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\Output
OutputBaseFilename=puretext-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add puretext to PATH (recommended)"; \
    GroupDescription: "Command line"; Flags: checkedonce

[Files]
Source: "..\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";          DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "..\LICENSE";            DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName} README"; Filename: "{app}\README.md"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Registry]
; Append the install directory to the per-user PATH when the task is selected.
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}"; Tasks: addtopath; \
    Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  { Avoid appending a second copy on reinstall. }
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;
