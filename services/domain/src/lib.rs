//! Offline-first domain model for the child-parent task loop.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role { Child, Parent }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskState { Assigned, Accepted, ProofSubmitted, AwaitingReview, Returned, Completed, Repairable }

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Proof { pub kind: String, pub content_ref: String }

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LedgerEntry { pub reason: String, pub delta: i32, pub balance_after: i32 }

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event { pub name: String, pub actor: Role }

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Task {
    pub id: String,
    pub state: TaskState,
    pub proof: Option<Proof>,
    pub why_answer: Option<String>,
    pub ledger: Vec<LedgerEntry>,
    pub events: Vec<Event>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DomainError { InvalidTransition, EmptyValue, RoleNotAllowed, WhyAlreadyAnswered }

impl Task {
    pub fn new(id: impl Into<String>) -> Result<Self, DomainError> {
        let id = id.into();
        if id.trim().is_empty() { return Err(DomainError::EmptyValue); }
        Ok(Self { id, state: TaskState::Assigned, proof: None, why_answer: None, ledger: Vec::new(), events: Vec::new() })
    }

    fn event(&mut self, name: &str, actor: Role) { self.events.push(Event { name: name.into(), actor }); }

    pub fn accept(&mut self, actor: Role) -> Result<(), DomainError> {
        if actor != Role::Child || self.state != TaskState::Assigned { return Err(if actor != Role::Child { DomainError::RoleNotAllowed } else { DomainError::InvalidTransition }); }
        self.state = TaskState::Accepted; self.event("accepted", actor); Ok(())
    }

    pub fn submit_proof(&mut self, actor: Role, kind: impl Into<String>, content_ref: impl Into<String>) -> Result<(), DomainError> {
        if actor != Role::Child || self.state != TaskState::Accepted { return Err(if actor != Role::Child { DomainError::RoleNotAllowed } else { DomainError::InvalidTransition }); }
        let kind = kind.into(); let content_ref = content_ref.into();
        if kind.trim().is_empty() || content_ref.trim().is_empty() { return Err(DomainError::EmptyValue); }
        self.proof = Some(Proof { kind, content_ref }); self.state = TaskState::AwaitingReview; self.event("proof_submitted", actor); Ok(())
    }

    pub fn review(&mut self, actor: Role, approve: bool, reason: impl Into<String>) -> Result<(), DomainError> {
        if actor != Role::Parent || self.state != TaskState::AwaitingReview { return Err(if actor != Role::Parent { DomainError::RoleNotAllowed } else { DomainError::InvalidTransition }); }
        let reason = reason.into(); if reason.trim().is_empty() { return Err(DomainError::EmptyValue); }
        if approve { self.state = TaskState::Completed; self.append_ledger("task_approved", 1); self.event("parent_approved", actor); }
        else { self.state = TaskState::Returned; self.event("parent_returned", actor); }
        Ok(())
    }

    pub fn answer_why(&mut self, actor: Role, answer: impl Into<String>) -> Result<(), DomainError> {
        if actor != Role::Child || self.state != TaskState::Completed { return Err(if actor != Role::Child { DomainError::RoleNotAllowed } else { DomainError::InvalidTransition }); }
        if self.why_answer.is_some() { return Err(DomainError::WhyAlreadyAnswered); }
        let answer = answer.into(); if answer.trim().is_empty() { return Err(DomainError::EmptyValue); }
        self.why_answer = Some(answer); self.event("why_answered", actor); Ok(())
    }

    pub fn repair(&mut self, actor: Role, note: impl Into<String>) -> Result<(), DomainError> {
        if actor != Role::Child || self.state != TaskState::Returned { return Err(if actor != Role::Child { DomainError::RoleNotAllowed } else { DomainError::InvalidTransition }); }
        let note = note.into(); if note.trim().is_empty() { return Err(DomainError::EmptyValue); }
        self.state = TaskState::Repairable; self.event("repair_started", actor); Ok(())
    }

    pub fn append_ledger(&mut self, reason: impl Into<String>, delta: i32) {
        let reason = reason.into();
        let prior = self.ledger.last().map(|entry| entry.balance_after).unwrap_or(0);
        self.ledger.push(LedgerEntry { reason, delta, balance_after: prior + delta });
    }

    pub fn balance(&self) -> i32 { self.ledger.last().map(|entry| entry.balance_after).unwrap_or(0) }
}

#[cfg(test)]
mod tests;
