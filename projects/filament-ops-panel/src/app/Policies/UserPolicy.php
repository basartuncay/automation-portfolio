<?php

namespace App\Policies;

use App\Enums\UserRole;
use App\Models\User;

/** Only admins manage users and roles; nobody can delete their own account from the panel. */
class UserPolicy
{
    public function viewAny(User $user): bool
    {
        return $user->hasRole(UserRole::Admin);
    }

    public function create(User $user): bool
    {
        return $user->hasRole(UserRole::Admin);
    }

    public function update(User $user, User $model): bool
    {
        return $user->hasRole(UserRole::Admin);
    }

    public function delete(User $user, User $model): bool
    {
        return $user->hasRole(UserRole::Admin) && $user->id !== $model->id;
    }
}
