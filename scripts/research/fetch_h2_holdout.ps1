# Public MOEX ISS; no account, key, or order APIs. Run from project root with pwsh 7.6+.
param([Parameter(Mandatory)][string]$OutDir)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$snapshot=Get-Content artifacts/historical-backtest-long-short/market-data.json -Raw | ConvertFrom-Json -DateKind String
$symbols=@($snapshot.securities.PSObject.Properties.Name)+@('IMOEX')
$dir=New-Item -ItemType Directory -Path $OutDir
foreach ($symbol in $symbols) {
    $route=if ($symbol -eq 'IMOEX') {'index/boards/SNDX'} else {'shares/boards/TQBR'}
    $start=0
    $result=@()
    do {
        $url="https://iss.moex.com/iss/engines/stock/markets/$route/securities/$symbol/candles.json?from=2026-08-17&till=2026-10-30&interval=24&iss.meta=off&iss.only=candles&start=$start"
        $response=Invoke-WebRequest -Uri $url -TimeoutSec 30
        $block=($response.Content | ConvertFrom-Json -DateKind String).candles
        if (!$block.columns) {throw "Missing candles block for $symbol"}
        foreach ($row in $block.data) {
            $v=@{}
            for ($i=0;$i -lt $block.columns.Count;$i++) {$v[$block.columns[$i]]=$row[$i]}
            $result+=@{trading_date=$v.begin.Substring(0,10); open=[string]$v.open; high=[string]$v.high; low=[string]$v.low; close=[string]$v.close; volume=[string]$v.volume}
        }
        $count=$block.data.Count
        $start+=$count
        if ($start -gt 85) {throw "Unexpected number of daily bars for $symbol"}
    } while ($count -gt 0)
    if ($result.Count -lt 2 -or $result[-1].trading_date -ne '2026-10-30') {throw "Incomplete $symbol"}
    [IO.File]::WriteAllText((Join-Path $dir.FullName "$symbol.json"),(ConvertTo-Json -InputObject $result))
    Write-Output "$symbol $start"
}
