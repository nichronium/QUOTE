"""
Domain-Agnostic Flat CSV Allocation Export Renderer for ERP / P2P Ingest.
Consumes pre-computed AwardRecordViewModel. Performs NO business calculations.
"""

from typing import Tuple
from artifacts.models import ArtifactFormat, ArtifactMetadata, ArtifactType
from artifacts.registry import BaseArtifactRenderer, register_renderer
from artifacts.view_models import AwardRecordViewModel


class ERPExportRenderer(BaseArtifactRenderer):
    artifact_type = ArtifactType.ERP_EXPORT

    def render(self, vm: AwardRecordViewModel, **kwargs) -> Tuple[bytes, ArtifactMetadata]:
        lines = [
            "rfq_id,rfq_line_id,item_sku,item_description,required_qty,required_uom,allocation_type,split_index,total_splits,supplier_name,supplier_id,quote_id,awarded_qty,unit_landed_cost,split_total_value,base_currency,comparison_id,award_id,award_status,finalized_at"
        ]

        for line in vm.lines:
            sku = line.item_sku.replace(",", ";")
            desc = line.item_description.replace(",", ";")
            req_qty = line.required_qty_display.replace(",", "")
            uom = line.canonical_uom

            if line.splits:
                for sp in line.splits:
                    sname = sp.supplier_name.replace(",", ";")
                    alloc_type = "SPLIT_SOURCING" if not sp.is_single else "SINGLE_SOURCING"
                    aqty = sp.awarded_qty_display.replace(",", "")
                    landed = sp.unit_landed_cost_display.replace(",", "")
                    sval = sp.split_value_display.replace(",", "")

                    lines.append(
                        f"{vm.rfq_id},{line.line_id},{sku},{desc},{req_qty},{uom},"
                        f"{alloc_type},{sp.split_index},{sp.total_splits},{sname},{sp.supplier_id},"
                        f"{sp.quote_id},{aqty},{landed},{sval},{vm.base_currency},"
                        f"{vm.comparison_id},{vm.award_id},{vm.status},{vm.finalized_at_display}"
                    )
            else:
                lines.append(
                    f"{vm.rfq_id},{line.line_id},{sku},{desc},{req_qty},{uom},"
                    f"UNALLOCATED,1,1,UNALLOCATED,N/A,N/A,0,0.00,0.00,{vm.base_currency},"
                    f"{vm.comparison_id},{vm.award_id},{vm.status},{vm.finalized_at_display}"
                )

        csv_content = "\n".join(lines)
        csv_bytes = csv_content.encode("utf-8")

        meta = ArtifactMetadata(
            artifact_id=f"ART-CSV-{vm.award_id}",
            award_id=vm.award_id,
            rfq_id=vm.rfq_id,
            artifact_type=ArtifactType.ERP_EXPORT,
            format=ArtifactFormat.CSV,
            name="ERP Allocation Export",
            filename=f"{vm.rfq_id}_Award_Allocation.csv",
            mime_type="text/csv",
            size_bytes=len(csv_bytes),
            download_url=f"/rfqs/{vm.rfq_id}/award/export/csv"
        )

        return csv_bytes, meta


register_renderer(ArtifactType.ERP_EXPORT, ERPExportRenderer)
