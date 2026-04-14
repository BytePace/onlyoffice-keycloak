from pydantic import BaseModel


class RecordFields(BaseModel):
    fields: dict[str, str]


class AddRecordsRequest(BaseModel):
    records: list[RecordFields]


class CreateDocRequest(BaseModel):
    name: str


class TableColumn(BaseModel):
    id: str
    type: str = "Text"


class TableDef(BaseModel):
    id: str
    columns: list[TableColumn] = []


class CreateTablesRequest(BaseModel):
    tables: list[TableDef]
