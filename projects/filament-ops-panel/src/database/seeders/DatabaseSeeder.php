<?php

namespace Database\Seeders;

use App\Models\Invoice;
use App\Models\User;
use App\Models\Vendor;
use Illuminate\Database\Seeder;

class DatabaseSeeder extends Seeder
{
    public function run(): void
    {
        // Demo login for local use only: admin@example.com / password
        User::factory()->create(['name' => 'Demo Admin', 'email' => 'admin@example.com']);
        User::factory()->create(['name' => 'Finance Lead', 'email' => 'finance@example.com']);

        foreach (Vendor::factory(8)->create() as $vendor) {
            Invoice::factory(fake()->numberBetween(3, 7))->for($vendor)->create();
        }
        Invoice::factory(4)->needsReview()->recycle(Vendor::all())->create();
    }
}
