<?php

namespace Database\Seeders;

use App\Enums\UserRole;
use App\Models\Invoice;
use App\Models\User;
use App\Models\Vendor;
use Illuminate\Database\Seeder;

class DatabaseSeeder extends Seeder
{
    public function run(): void
    {
        // Demo logins for local use only, all with password "password": one per role
        User::factory()->create(['name' => 'Demo Admin', 'email' => 'admin@example.com', 'role' => UserRole::Admin]);
        User::factory()->create(['name' => 'Alex Approver', 'email' => 'approver@example.com', 'role' => UserRole::Approver]);
        User::factory()->create(['name' => 'Pat Payer', 'email' => 'payer@example.com', 'role' => UserRole::Payer]);
        User::factory()->create(['name' => 'Vic Viewer', 'email' => 'viewer@example.com', 'role' => UserRole::Viewer]);
        // Service account that owns the ingestion API token (see `php artisan invoices:token`)
        User::factory()->create(['name' => 'Intake bot', 'email' => 'intake@example.com', 'role' => UserRole::Viewer]);

        foreach (Vendor::factory(8)->create() as $vendor) {
            Invoice::factory(fake()->numberBetween(3, 7))->for($vendor)->create();
        }
        Invoice::factory(4)->needsReview()->recycle(Vendor::all())->create();
    }
}
