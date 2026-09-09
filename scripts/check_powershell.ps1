$ErrorActionPreference='Stop'
$repo=Split-Path $PSScriptRoot -Parent
$extract=@'
import ast, importlib.util, json, pathlib, sys
sys.dont_write_bytecode = True
path = pathlib.Path(sys.argv[1]) / 'setup.py'
tree = ast.parse(path.read_text(encoding='utf-8'))
samples = [node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'ps' and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)]
spec = importlib.util.spec_from_file_location('setup_fixture', path)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
stale = 'http://srt:fixture@localhost:64321'
samples.append(setup.profile_cleanup('$env:HTTP_PROXY="' + stale + '"\n', {stale}))
print(json.dumps(samples))
'@
$all=& python -X utf8 -c $extract $repo | ConvertFrom-Json
if($LASTEXITCODE -ne 0){throw 'Could not extract embedded PowerShell scripts.'}
$i=0
foreach($script in $all){
    $tokens=$null; $parseErrors=$null
    $null=[System.Management.Automation.Language.Parser]::ParseInput($script,[ref]$tokens,[ref]$parseErrors)
    if($parseErrors.Count -gt 0){throw ('Invalid embedded PowerShell sample '+$i+': '+($parseErrors | Out-String))}
    $i++
}
$tokens=$null; $parseErrors=$null
$bootstrap=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repo 'bootstrap.ps1'),[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count -gt 0){throw ($parseErrors | Out-String)}
$functions=$bootstrap.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)
foreach($fn in $functions){ . ([scriptblock]::Create($fn.Extent.Text)) }
$kaizoLog=Join-Path ([IO.Path]::GetTempPath()) ('kaizo-bootstrap-log-check-' + [Guid]::NewGuid().ToString('N') + '.log')
try {
    Write-SetupLog 'ERROR' 'stage=verify-runtime HRESULT=0x80070005 sk-fixture-secret socks5://user:fixture-password@localhost:9 Bearer fixture-bearer refresh_token="fixture-refresh" api_key=fixture-key'
    $logged=Get-Content -Raw -LiteralPath $kaizoLog
    if($logged -match 'fixture-(secret|password|bearer|refresh|key)'){throw 'Bootstrap log leaked fixture credentials.'}
    if($logged -notmatch 'ERROR BOOTSTRAP stage=verify-runtime HRESULT=0x80070005'){throw 'Bootstrap log lost diagnostics.'}
    if($logged -notmatch '\[KEY\]' -or $logged -notmatch '\[TOKEN\]' -or $logged -notmatch '\[CREDENTIALS\]'){throw 'Bootstrap log redaction failed.'}
} finally { Remove-Item -LiteralPath $kaizoLog -Force -ErrorAction SilentlyContinue }
$old=[Environment]::GetEnvironmentVariable('HTTP_PROXY','Process')
try {
    & ([scriptblock]::Create($all[-1]))
    if([Environment]::GetEnvironmentVariable('HTTP_PROXY','Process')){throw 'Expired proxy survived profile cleanup.'}
    $env:HTTP_PROXY='http://srt:fresh@localhost:1234'
    $cleanup=$all[-1].Substring($all[-1].IndexOf('# KAIZO'))
    & ([scriptblock]::Create($cleanup))
    if($env:HTTP_PROXY -ne 'http://srt:fresh@localhost:1234'){throw 'A different proxy was removed.'}
} finally {
    [Environment]::SetEnvironmentVariable('HTTP_PROXY',$old,'Process')
}
Write-Output ('PASS: bootstrap + '+$i+' embedded PowerShell scripts parsed; bootstrap logs redacted; expired proxy removed and fresh proxy preserved.')
