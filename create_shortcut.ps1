$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs = Join-Path $dir "run.vbs"
$ico = Join-Path $dir "icon.ico"
$ws  = New-Object -ComObject WScript.Shell

$destinations = @(
    [System.IO.Path]::Combine([System.Environment]::GetFolderPath('Desktop'), 'PlotCut.lnk'),
    [System.IO.Path]::Combine($dir, 'PlotCut.lnk')
)

foreach ($dest in $destinations) {
    $s = $ws.CreateShortcut($dest)
    $s.TargetPath       = $vbs
    $s.IconLocation     = $ico
    $s.Description      = "PlotCut"
    $s.WorkingDirectory = $dir
    $s.Save()
    Write-Host "  Shortcut created: $dest"
}
