use super::*;

#[test]
fn happy_path_is_append_only_and_balanced() {
    let mut task = Task::new("task-1").unwrap();
    task.accept(Role::Child).unwrap();
    task.submit_proof(Role::Child, "photo", "local://proof-1").unwrap();
    task.review(Role::Parent, true, "looks good").unwrap();
    task.answer_why(Role::Child, "because practice builds confidence").unwrap();
    assert_eq!(task.state, TaskState::Completed);
    assert_eq!(task.balance(), 1);
    assert_eq!(task.events.iter().map(|e| e.name.as_str()).collect::<Vec<_>>(), vec!["accepted", "proof_submitted", "parent_approved", "why_answered"]);
}

#[test]
fn return_then_repair_is_reversible() {
    let mut task = Task::new("task-2").unwrap();
    task.accept(Role::Child).unwrap();
    task.submit_proof(Role::Child, "voice", "local://proof-2").unwrap();
    task.review(Role::Parent, false, "show one more step").unwrap();
    task.repair(Role::Child, "try again").unwrap();
    assert_eq!(task.state, TaskState::Repairable);
    assert_eq!(task.balance(), 0);
}

#[test]
fn invalid_roles_and_transitions_are_rejected() {
    let mut task = Task::new("task-3").unwrap();
    assert_eq!(task.accept(Role::Parent), Err(DomainError::RoleNotAllowed));
    assert_eq!(task.review(Role::Parent, true, "no proof"), Err(DomainError::InvalidTransition));
}

#[test]
fn role_boundaries_and_duplicate_why_are_enforced() {
    let mut task = Task::new("task-4").unwrap();
    task.accept(Role::Child).unwrap();
    task.submit_proof(Role::Child, "checklist", "local://proof-4").unwrap();
    assert_eq!(task.review(Role::Child, true, "forbidden"), Err(DomainError::RoleNotAllowed));
    task.review(Role::Parent, true, "approved").unwrap();
    task.answer_why(Role::Child, "because it helps").unwrap();
    assert_eq!(task.answer_why(Role::Child, "again"), Err(DomainError::WhyAlreadyAnswered));
    let ledger_len = task.ledger.len();
    task.append_ledger("manual_repair", 2);
    assert_eq!(task.ledger.len(), ledger_len + 1);
    assert_eq!(task.balance(), 3);
}
