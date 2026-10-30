"""Physical traceability: device registry, passports, QR codes."""
from .devices import (
    affected_by_component,
    affected_by_revision,
    list_devices,
    passport,
    register_device,
    sign_device,
    verify_device,
)
from .qr import make_qr, qr_svg

__all__ = ["affected_by_component", "affected_by_revision", "list_devices", "passport",
           "register_device", "sign_device", "verify_device", "make_qr", "qr_svg"]
