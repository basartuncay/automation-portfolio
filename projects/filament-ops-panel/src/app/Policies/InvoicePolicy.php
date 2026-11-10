<?php

namespace App\Policies;

use App\Enums\InvoiceStatus;
use App\Enums\UserRole;
use App\Models\Invoice;
use App\Models\User;

/** Segregation of duties: approvers approve, payers pay, and nobody pays an invoice they approved themselves. */
class InvoicePolicy
{
    public function viewAny(User $user): bool
    {
        return true;
    }

    public function view(User $user, Invoice $invoice): bool
    {
        return true;
    }

    public function create(User $user): bool
    {
        return $user->hasRole(UserRole::Approver, UserRole::Admin);
    }

    public function update(User $user, Invoice $invoice): bool
    {
        $open = in_array($invoice->status, [InvoiceStatus::Pending, InvoiceStatus::NeedsReview], true);

        return $open && $user->hasRole(UserRole::Approver, UserRole::Admin);
    }

    public function approve(User $user, Invoice $invoice): bool
    {
        return $invoice->status->canBecome(InvoiceStatus::Approved) && $user->hasRole(UserRole::Approver, UserRole::Admin);
    }

    public function reject(User $user, Invoice $invoice): bool
    {
        return $invoice->status->canBecome(InvoiceStatus::Rejected) && $user->hasRole(UserRole::Approver, UserRole::Admin);
    }

    public function pay(User $user, Invoice $invoice): bool
    {
        return $invoice->status->canBecome(InvoiceStatus::Paid)
            && $user->hasRole(UserRole::Payer, UserRole::Admin)
            && $invoice->approved_by !== $user->id; // four-eyes: the approver can't also release the payment
    }

    public function delete(User $user, Invoice $invoice): bool
    {
        return $user->hasRole(UserRole::Admin) && $invoice->status !== InvoiceStatus::Paid;
    }

    public function deleteAny(User $user): bool
    {
        return $user->hasRole(UserRole::Admin);
    }
}
