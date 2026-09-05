"""Real HTTP creation and projection contracts for the operator board."""
from tests.plugins.test_kanban_dashboard_plugin import client, kanban_home


def test_report_round_trip_and_board_projection(client):
    response = client.post("/api/plugins/kanban/tasks", json={"title":"Audit users", "assignee":"default", "requires_repo":False, "delivery_type":"report"})
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["requires_repo"] is False and task["delivery_type"] == "report"
    assert task["is_executing"] is False
    response = client.get("/api/plugins/kanban/board")
    tasks = [t for c in response.json()["columns"] for t in c["tasks"]]
    card = next(t for t in tasks if t["id"] == task["id"])
    assert card["board_column"] == "todo"
    assert card["task_role"] == "work"
    response = client.patch("/api/plugins/kanban/tasks/" + task["id"], json={"status":"done", "result":"Verified report with source and target evidence"})
    assert response.status_code == 200, response.text
    assert response.json()["task"]["board_column"] == "done"
