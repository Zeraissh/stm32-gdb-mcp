cmake_minimum_required(VERSION 3.20)

project(stm32l151_faultdemo C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

set(TARGET_NAME stm32l151_faultdemo)
set(LINKER_SCRIPT ${CMAKE_CURRENT_SOURCE_DIR}/linker/STM32L151RCTx_FLASH.ld)

add_executable(${TARGET_NAME}
    src/main.c
    src/startup_stm32l1xx.c
)

set_target_properties(${TARGET_NAME} PROPERTIES SUFFIX ".elf")

target_compile_definitions(${TARGET_NAME} PRIVATE STM32L151xC)

# STM32L151 = Cortex-M3, no FPU (soft float)
target_compile_options(${TARGET_NAME} PRIVATE
    -mcpu=cortex-m3
    -mthumb
    -mfloat-abi=soft
    -ffunction-sections
    -fdata-sections
    -Wall
    -Wextra
    -Werror
    -Og
    -g3
)

target_link_options(${TARGET_NAME} PRIVATE
    -mcpu=cortex-m3
    -mthumb
    -mfloat-abi=soft
    -T${LINKER_SCRIPT}
    -Wl,-Map=${TARGET_NAME}.map
    -Wl,--gc-sections
    -Wl,--print-memory-usage
    -nostartfiles
)

add_custom_command(TARGET ${TARGET_NAME} POST_BUILD
    COMMAND ${CMAKE_OBJCOPY} -O binary $<TARGET_FILE:${TARGET_NAME}> ${TARGET_NAME}.bin
    BYPRODUCTS ${TARGET_NAME}.bin
    COMMENT "Generating ${TARGET_NAME}.bin"
)
