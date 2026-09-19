; Inno Setup script -- Windows installer for vanilla_extract.
;
; Build order:
;     python packaging\build_standalone.py      -> dist\vanilla.exe
;     iscc packaging\vanilla-extract.iss               -> Output\vanilla_extract-setup-x.y.z.exe
;
; Installs the single executable and puts it on PATH, so `vanilla` works from
; any Command Prompt or PowerShell window. Per-user install by default: it needs
; no administrator rights, which is what makes it usable on a locked-down
; corporate machine -- the same audience that cannot pip install anything.

#define AppName "vanilla_extract"
#define AppVersion "0.2.0"
#define AppPublisher "Brandon S. Butler"
#define AppURL "https://github.com/brandonsbutler-ai/vanilla-extract"
#define AppExeName "vanilla.exe"

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
OutputBaseFilename=vanilla_extract-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}
; The [Registry] entry below edits the user's PATH. Without this, running
; Command Prompt and Explorer windows are not told, and the change -- the add on
; install, the removal on uninstall -- only shows up after the next sign-in.
ChangesEnvironment=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add vanilla_extract to PATH (recommended)"; \
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

{ The [Registry] entry adds the install folder to the user's PATH but nothing
  removed it: an uninstall left a PATH entry pointing at a folder that no
  longer exists. Take exactly that entry back out, whatever its position, and
  leave the rest of the user's PATH as it was. }
procedure RemovePath(Dir: string);
var
  Path: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path) then
    exit;
  { Separators on both ends, so the first and last entries match like any other. }
  Path := ';' + Path + ';';
  P := Pos(';' + Uppercase(Dir) + ';', Uppercase(Path));
  if P = 0 then
    exit;
  Delete(Path, P, Length(Dir) + 1);
  Path := Copy(Path, 2, Length(Path) - 2);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemovePath(ExpandConstant('{app}'));
end;
