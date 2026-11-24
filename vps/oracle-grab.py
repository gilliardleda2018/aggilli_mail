#!/usr/bin/env python3
"""Tenta criar a VM Always Free na Oracle até conseguir capacidade.

Usa ~/.oci/config. Cria (se não existir) a rede pública com as portas de email liberadas,
depois tenta lançar a VM alternando entre Ampere A1 (1 OCPU/6 GB) e AMD E2.1.Micro.
Só cria recursos Always Free.

Uso: vps/.venv/bin/python vps/oracle-grab.py [--once] [--intervalo 90]
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import oci

NAME = "mail-vps"
VCN_NAME = "mail-vcn"
SUBNET_NAME = "mail-subnet"
PORTS = [22, 25, 465, 587, 993]
SSH_KEY = (Path(__file__).parent / "id_ed25519.pub").read_text().strip()
SHAPES = [
    ("VM.Standard.A1.Flex", {"ocpus": 1.0, "memory_in_gbs": 6.0}),
    ("VM.Standard.E2.1.Micro", None),
]


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def ensure_network(net, comp):
    vcns = [v for v in net.list_vcns(comp).data if v.display_name == VCN_NAME
            and v.lifecycle_state == "AVAILABLE"]
    if vcns:
        vcn = vcns[0]
    else:
        log("criando VCN")
        vcn = net.create_vcn(oci.core.models.CreateVcnDetails(
            compartment_id=comp, display_name=VCN_NAME, cidr_blocks=["10.0.0.0/16"],
            dns_label="mailvcn")).data
        vcn = oci.wait_until(net, net.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE").data

    igws = net.list_internet_gateways(comp, vcn_id=vcn.id).data
    igw = igws[0] if igws else net.create_internet_gateway(
        oci.core.models.CreateInternetGatewayDetails(
            compartment_id=comp, vcn_id=vcn.id, is_enabled=True, display_name="mail-igw")).data

    rt = net.get_route_table(vcn.default_route_table_id).data
    if not any(r.destination == "0.0.0.0/0" for r in rt.route_rules):
        net.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(
            route_rules=rt.route_rules + [oci.core.models.RouteRule(
                destination="0.0.0.0/0", destination_type="CIDR_BLOCK",
                network_entity_id=igw.id)]))

    sl = net.get_security_list(vcn.default_security_list_id).data
    have = {(r.tcp_options.destination_port_range.min if r.tcp_options and
             r.tcp_options.destination_port_range else None) for r in sl.ingress_security_rules}
    new_rules = [oci.core.models.IngressSecurityRule(
        protocol="6", source="0.0.0.0/0",
        tcp_options=oci.core.models.TcpOptions(
            destination_port_range=oci.core.models.PortRange(min=p, max=p)))
        for p in PORTS if p not in have]
    if new_rules:
        log(f"liberando portas {[r.tcp_options.destination_port_range.min for r in new_rules]}")
        net.update_security_list(sl.id, oci.core.models.UpdateSecurityListDetails(
            ingress_security_rules=sl.ingress_security_rules + new_rules,
            egress_security_rules=sl.egress_security_rules))

    subnets = [s for s in net.list_subnets(comp, vcn_id=vcn.id).data
               if s.lifecycle_state == "AVAILABLE"]
    if subnets:
        return subnets[0]
    log("criando subnet pública")
    sn = net.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=comp, vcn_id=vcn.id, cidr_block="10.0.0.0/24",
        display_name=SUBNET_NAME, dns_label="mailsubnet",
        prohibit_public_ip_on_vnic=False)).data
    return oci.wait_until(net, net.get_subnet(sn.id), "lifecycle_state", "AVAILABLE").data


def ubuntu_image(compute, comp, shape):
    imgs = compute.list_images(
        comp, operating_system="Canonical Ubuntu", operating_system_version="24.04",
        shape=shape, sort_by="TIMECREATED", sort_order="DESC").data
    if not imgs:
        raise RuntimeError(f"sem imagem Ubuntu 24.04 para {shape}")
    return imgs[0].id


def existing_instance(compute, comp):
    for i in compute.list_instances(comp, display_name=NAME).data:
        if i.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            return i
    return None


def public_ip(compute, net, comp, inst):
    for att in compute.list_vnic_attachments(comp, instance_id=inst.id).data:
        ip = net.get_vnic(att.vnic_id).data.public_ip
        if ip:
            return ip
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="só uma rodada de tentativas")
    ap.add_argument("--intervalo", type=int, default=90, help="segundos entre rodadas")
    args = ap.parse_args()

    cfg = oci.config.from_file()
    oci.config.validate_config(cfg)
    comp = cfg["tenancy"]
    ident = oci.identity.IdentityClient(cfg)
    compute = oci.core.ComputeClient(cfg)
    net = oci.core.VirtualNetworkClient(cfg)

    ad = ident.list_availability_domains(comp).data[0].name
    log(f"região {cfg['region']}, {ad}")

    inst = existing_instance(compute, comp)
    if not inst:
        subnet = ensure_network(net, comp)
        images = {s: ubuntu_image(compute, comp, s) for s, _ in SHAPES}
        tentativa = 0
        while not inst:
            tentativa += 1
            for shape, cfg_shape in SHAPES:
                try:
                    inst = compute.launch_instance(oci.core.models.LaunchInstanceDetails(
                        compartment_id=comp, availability_domain=ad, display_name=NAME,
                        shape=shape,
                        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(**cfg_shape)
                        if cfg_shape else None,
                        source_details=oci.core.models.InstanceSourceViaImageDetails(
                            image_id=images[shape]),
                        create_vnic_details=oci.core.models.CreateVnicDetails(
                            subnet_id=subnet.id, assign_public_ip=True),
                        metadata={"ssh_authorized_keys": SSH_KEY},
                    )).data
                    log(f"CONSEGUIU: {shape}")
                    break
                except oci.exceptions.ServiceError as e:
                    if e.status in (500, 429) or "capacity" in str(e.message).lower():
                        log(f"tentativa {tentativa}: {shape} sem capacidade ({e.status})")
                        continue
                    raise
            if inst or args.once:
                break
            time.sleep(args.intervalo)
        if not inst:
            log("sem capacidade nesta rodada")
            return 1

    log(f"instância {inst.display_name} ({inst.shape}), aguardando RUNNING…")
    inst = oci.wait_until(compute, compute.get_instance(inst.id), "lifecycle_state", "RUNNING",
                          max_wait_seconds=900).data
    ip = public_ip(compute, net, comp, inst)
    log(f"PRONTA. IP público: {ip}")
    Path(__file__).with_name("vm-ip.txt").write_text(f"{ip}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
