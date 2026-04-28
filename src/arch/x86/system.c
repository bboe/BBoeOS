// system.c — PC-specific reboot and shutdown.
//
// reboot:   pulse the 8042 reset line.  Drains the input buffer first
//           so the 0xFE command isn't dropped, then halts in case the
//           reset lags.  Never returns.
// shutdown: tries QEMU ACPI (0x604) and the Bochs/old-QEMU port
//           (0xB004) in turn.  Returns if neither responds — caller
//           treats that as "shutdown not supported".

#define ACPI_SHUTDOWN_PORT          0x0604
#define BOCHS_SHUTDOWN_PORT         0xB004
#define KEYBOARD_CONTROLLER_COMMAND 0x64
#define KEYBOARD_CONTROLLER_RESET   0xFE
#define KEYBOARD_CONTROLLER_STATUS_INPUT_FULL 0x02
#define SHUTDOWN_VALUE              0x2000

void reboot() {
    asm("cli");
    while ((kernel_inb(KEYBOARD_CONTROLLER_COMMAND) & KEYBOARD_CONTROLLER_STATUS_INPUT_FULL) != 0) {}
    kernel_outb(KEYBOARD_CONTROLLER_COMMAND, KEYBOARD_CONTROLLER_RESET);
    while (1) {
        asm("hlt");
    }
}

void shutdown() {
    kernel_outw(ACPI_SHUTDOWN_PORT, SHUTDOWN_VALUE);
    kernel_outw(BOCHS_SHUTDOWN_PORT, SHUTDOWN_VALUE);
}
