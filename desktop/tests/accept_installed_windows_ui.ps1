# Interact with the installed client's WebView2 through the native accessibility tree.
param(
    [Parameter(Mandatory)]
    [int]$ClientPid,
    [switch]$SwitchToEnglish
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Wait-VisibleControl([string]$Name, [int]$TimeoutSeconds) {
    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty, $Name
    )
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $process = Get-Process -Id $ClientPid
        if ($process.MainWindowHandle -ne [IntPtr]::Zero) {
            try {
                $window = [System.Windows.Automation.AutomationElement]::FromHandle(
                    $process.MainWindowHandle
                )
                $control = $window.FindFirst(
                    [System.Windows.Automation.TreeScope]::Descendants, $condition
                )
                if ($null -ne $control -and -not $control.Current.IsOffscreen -and $control.Current.IsEnabled) {
                    return $control
                }
            } catch [System.Windows.Automation.ElementNotAvailableException] {
                # WebView2 can replace its accessibility elements during initial navigation.
            }
        }
        Start-Sleep -Milliseconds 250
    }
    throw "The installed client's window did not show '$Name'."
}

if ($SwitchToEnglish) {
    $language = Wait-VisibleControl 'English' 50
    $invoke = $language.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $invoke.Invoke()
}
$null = Wait-VisibleControl 'Employee sign in' 50
