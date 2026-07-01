import xml.etree.ElementTree as ET


class SVDParser:
    def __init__(self):
        self.svd_root = None

    def load(self, filepath: str):
        tree = ET.parse(filepath)
        self.svd_root = tree.getroot()

    def get_register_address(self, peripheral_name: str, register_name: str):
        return self.get_register(peripheral_name, register_name)["address_int"]

    def get_register(self, peripheral_name: str, register_name: str):
        peripheral = self._find_peripheral(peripheral_name)
        base_addr = int(self._child_text(peripheral, "baseAddress"), 0)
        register = self._find_register(peripheral, register_name)
        resolved_register = self._resolve_derived_register(peripheral, register)
        offset = int(self._child_text(register, "addressOffset"), 0)

        return {
            "peripheral": peripheral_name,
            "register": register_name,
            "address_int": base_addr + offset,
            "description": self._child_text(resolved_register, "description", ""),
            "size": int(self._child_text(resolved_register, "size", "32"), 0),
            "fields": self._parse_fields(resolved_register),
        }

    def interrupt_numbers(self) -> dict:
        """Return ``{interrupt_name: irq_number}`` for every ``<interrupt>`` in the SVD.

        The SVD lists each device IRQ under its owning peripheral as
        ``<interrupt><name>..</name><value>..</value></interrupt>``. This exposes that
        table so a derived AcceptanceSpec can place NVIC ISER bits from a resolved IRQ
        name. Returns ``{}`` when no SVD is loaded; skips any malformed entry.
        """
        numbers: dict = {}
        if self.svd_root is None:
            return numbers
        for periph in self._children_by_path(self.svd_root, ["peripherals", "peripheral"]):
            for interrupt in self._children_by_path(periph, ["interrupt"]):
                name = self._child_text(interrupt, "name")
                raw = self._child_text(interrupt, "value")
                if name is None or raw is None:
                    continue
                try:
                    numbers[name] = int(raw, 0)
                except (TypeError, ValueError):
                    continue
        return numbers

    def decode_register_value(self, peripheral_name: str, register_name: str, value: int):
        register = self.get_register(peripheral_name, register_name)
        fields = []
        for field in register["fields"]:
            mask = (1 << field["bit_width"]) - 1
            raw = (value >> field["bit_offset"]) & mask
            decoded = {
                "name": field["name"],
                "bit_offset": field["bit_offset"],
                "bit_width": field["bit_width"],
                "bit_range": self._format_bit_range(field["bit_offset"], field["bit_width"]),
                "raw": raw,
                "hex": hex(raw),
            }
            if raw in field["enumerated_values"]:
                decoded["meaning"] = field["enumerated_values"][raw]
            fields.append(decoded)

        return {
            "peripheral": peripheral_name,
            "register": register_name,
            "address": hex(register["address_int"]),
            "value": f"0x{value & 0xFFFFFFFF:08x}",
            "fields": fields,
        }

    def _find_peripheral(self, peripheral_name: str):
        if self.svd_root is None:
            raise RuntimeError("SVD file not loaded")

        for periph in self._children_by_path(self.svd_root, ["peripherals", "peripheral"]):
            if self._child_text(periph, "name") == peripheral_name:
                return periph
        raise ValueError(f"Could not find peripheral {peripheral_name} in SVD")

    def _find_register(self, peripheral, register_name: str):
        for reg in self._children_by_path(peripheral, ["registers", "register"]):
            if self._child_text(reg, "name") == register_name:
                return reg
        raise ValueError(f"Could not find register {register_name} in SVD")

    def _resolve_derived_register(self, peripheral, register):
        derived_from = register.attrib.get("derivedFrom")
        if not derived_from:
            return register
        base_register = self._find_register(peripheral, derived_from)
        return self._merge_register(base_register, register)

    def _merge_register(self, base_register, override_register):
        merged = ET.Element(base_register.tag, base_register.attrib)
        for child in list(base_register):
            merged.append(child)
        existing_tags = {self._local_name(child.tag) for child in merged}
        for child in list(override_register):
            tag = self._local_name(child.tag)
            if tag in existing_tags:
                for old_child in list(merged):
                    if self._local_name(old_child.tag) == tag:
                        merged.remove(old_child)
                        break
            merged.append(child)
        return merged

    def _parse_fields(self, register):
        fields = []
        for field in self._children_by_path(register, ["fields", "field"]):
            name = self._child_text(field, "name")
            bit_offset, bit_width = self._field_bit_range(field)
            fields.append({
                "name": name,
                "bit_offset": bit_offset,
                "bit_width": bit_width,
                "description": self._child_text(field, "description", ""),
                "enumerated_values": self._parse_enumerated_values(field),
            })
        return fields

    def _parse_enumerated_values(self, field):
        values = {}
        for enum in self._children_by_path(field, ["enumeratedValues", "enumeratedValue"]):
            raw_value = self._child_text(enum, "value")
            if raw_value is None:
                continue
            values[int(raw_value, 0)] = self._child_text(enum, "name", "")
        return values

    def _field_bit_range(self, field):
        bit_offset = self._child_text(field, "bitOffset")
        bit_width = self._child_text(field, "bitWidth")
        if bit_offset is not None and bit_width is not None:
            return int(bit_offset, 0), int(bit_width, 0)

        bit_range = self._child_text(field, "bitRange")
        if bit_range:
            high, low = bit_range.strip("[]").split(":")
            high_int = int(high, 0)
            low_int = int(low, 0)
            return low_int, high_int - low_int + 1

        lsb = self._child_text(field, "lsb")
        msb = self._child_text(field, "msb")
        if lsb is not None and msb is not None:
            lsb_int = int(lsb, 0)
            msb_int = int(msb, 0)
            return lsb_int, msb_int - lsb_int + 1

        raise ValueError(f"Field {self._child_text(field, 'name', '<unnamed>')} has no bit range")

    def _children_by_path(self, element, names):
        current = [element]
        for name in names:
            next_level = []
            for node in current:
                next_level.extend(
                    child for child in list(node)
                    if self._local_name(child.tag) == name
                )
            current = next_level
        return current

    def _child_text(self, element, name: str, default=None):
        for child in list(element):
            if self._local_name(child.tag) == name:
                return child.text
        return default

    def _local_name(self, tag: str):
        return tag.rsplit("}", 1)[-1]

    def _format_bit_range(self, bit_offset: int, bit_width: int):
        high = bit_offset + bit_width - 1
        if high == bit_offset:
            return str(bit_offset)
        return f"{high}:{bit_offset}"
