ENTRY(Reset_Handler)

/* STM32L151xC (Cat.3, dev_id 0x427): 256K flash, 32K SRAM */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 256K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 32K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);

SECTIONS
{
  .isr_vector :
  {
    KEEP(*(.isr_vector))
  } > FLASH

  .text :
  {
    *(.text*)
    *(.rodata*)
    KEEP(*(.init))
    KEEP(*(.fini))
    . = ALIGN(4);
    _etext = .;
  } > FLASH

  .data : AT(_etext)
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } > RAM

  .bss :
  {
    . = ALIGN(4);
    _sbss = .;
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > RAM

  .noinit (NOLOAD) :
  {
    . = ALIGN(4);
    _snoinit = .;
    KEEP(*(.noinit_head))
    *(.noinit*)
    . = ALIGN(4);
    _enoinit = .;
  } > RAM

  ._user_heap_stack :
  {
    . = ALIGN(8);
    . = . + 0x400;
    . = . + 0x400;
    . = ALIGN(8);
  } > RAM
}
