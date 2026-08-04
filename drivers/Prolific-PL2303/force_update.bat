pnputil /delete-driver oem50.inf /uninstall /force > "C:\Stinger\drivers\Prolific-PL2303\force_update.log" 2>&1
echo DELETE_EXIT:%ERRORLEVEL% >> "C:\Stinger\drivers\Prolific-PL2303\force_update.log"
pnputil /add-driver "C:\Stinger\drivers\Prolific-PL2303\v3.9.0.2\Prolific Treiber Version 3.9.0.2\SER2PL_1.inf" /install >> "C:\Stinger\drivers\Prolific-PL2303\force_update.log" 2>&1
echo ADD_EXIT:%ERRORLEVEL% >> "C:\Stinger\drivers\Prolific-PL2303\force_update.log"
pnputil /update-driver "USB\VID_067B&PID_2303\5&10F2F38D&0&5" "C:\Stinger\drivers\Prolific-PL2303\v3.9.0.2\Prolific Treiber Version 3.9.0.2\SER2PL_1.inf" >> "C:\Stinger\drivers\Prolific-PL2303\force_update.log" 2>&1
echo UPDATE_EXIT:%ERRORLEVEL% >> "C:\Stinger\drivers\Prolific-PL2303\force_update.log"
