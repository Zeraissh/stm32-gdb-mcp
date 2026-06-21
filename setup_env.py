import shutil

# Define tools and their installation instructions
TOOLS = {
    "arm-none-eabi-gdb": {
        "name": "ARM GNU Toolchain (GDB)",
        "install_guide": "Download from https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads and add the 'bin' folder to PATH."
    },
    "openocd": {
        "name": "OpenOCD",
        "install_guide": "Download xPack OpenOCD from https://github.com/xpack-dev-tools/openocd-xpack/releases, extract it, and add the 'bin' folder to PATH."
    },
    "JLinkGDBServerCL": {
        "name": "J-Link GDB Server",
        "install_guide": "Download the J-Link Software and Documentation Pack from https://www.segger.com/downloads/jlink/ and add the installation folder to PATH."
    },
    "st-util": {
        "name": "ST-LINK GDB Server (st-link tools)",
        "install_guide": "Download from https://github.com/stlink-org/stlink/releases, extract, and add the 'bin' folder to PATH."
    }
}

def check_env():
    print("=" * 60)
    print(" STM32 GDB MCP Server - Environment Setup Check")
    print("=" * 60)
    
    missing_tools = []
    
    for exe, info in TOOLS.items():
        # JLinkGDBServer has different names, try common ones
        if exe == "JLinkGDBServerCL":
            path = shutil.which("JLinkGDBServerCL") or shutil.which("JLinkGDBServer") or shutil.which("jlinkgdbserver")
        else:
            path = shutil.which(exe)
            
        if path:
            print(f"[OK] {info['name']} found at: {path}")
        else:
            print(f"[MISSING] {info['name']} ({exe}) is NOT found in PATH.")
            missing_tools.append(info)
            
    print("-" * 60)
    
    if not missing_tools:
        print("All required tools are installed and available in PATH. You are ready to go!")
    else:
        print("Some tools are missing. Please follow the instructions below to install them:")
        for tool in missing_tools:
            print(f"\n- {tool['name']}:")
            print(f"  {tool['install_guide']}")
            
        print("\n[NOTE] After installing, please ensure you restart your terminal to apply the PATH changes.")

if __name__ == "__main__":
    check_env()
